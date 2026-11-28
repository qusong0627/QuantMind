import axios, { AxiosInstance } from 'axios';
import { SERVICE_ENDPOINTS, resolveWebSafeServiceBase } from '../config/services';
import { authService } from '../features/auth/services/authService';
import {
  AdminModelFeatureCatalog,
  QuantDBTrainingSourcesResult,
} from '../features/admin/types';
import type {
  BackfillJob,
  BackfillJobList,
  BackfillPreview,
} from './modelInferenceJobTypes';
import type { SignalEvalState } from './modelSignalEvalTypes';
import type { RetrainConfigPayload } from '../pages/training/retrainConfig';
import type {
  LabelTargetSummary,
  TrainingCapabilities,
} from '../pages/training/trainingTargetCapabilities';

export interface LabelTargetListResponse {
  items: LabelTargetSummary[];
  count: number;
}

export interface ModelTrainingRunResponse {
  runId: string;
  status: string;
  payload?: Record<string, unknown>;
}

export interface ModelTrainingRunStatus {
  runId: string;
  status: string;
  progress: number;
  logs: string;
  result?: Record<string, unknown>;
  isCompleted: boolean;
}

/** WP2 严格评估：单个 Top-N 的成本块（15/25/40bp 各一份） */
export interface StrictEvalCostBlock {
  total: number;
  annualized: number;
  mdd: number;
  win_batches: number;
  avg_ret: number;
  excess_hs300: number;
  excess_zz1000: number;
  t_stat: number;
  hac_t: number;
  bootstrap_ci: [number, number];
  psr: number;
  monthly: Array<{ month: string; ret: number }>;
}

export interface StrictEvalTopBlock {
  batches: number;
  gross_mean: number;
  win_batches: number;
  overlap_prev: number | null;
  turnover_per_batch: number | null;
  bench_hs300: number;
  bench_zz1000: number;
  participation: {
    n: number;
    median?: number;
    p95?: number;
    max?: number;
    over_10pct?: number;
  };
  signal_dates: string[];
  batch_returns_gross: number[];
  monthly_gross: Array<{ month: string; ret: number }>;
  [costKey: string]: unknown;
}

export interface StrictEvalReportRow {
  run_id: string;
  model_id?: string | null;
  market: string;
  horizon_days: number;
  entry_price: string;
  status: 'ready' | 'running' | 'failed' | string;
  error?: string;
  generated_at?: string;
  report: {
    version: number;
    run_id: string;
    market: string;
    horizon_days: number;
    entry_price: string;
    cost_bps: number[];
    segments: Record<string, { start: string; end: string }>;
    results: Record<string, Record<string, StrictEvalTopBlock>>;
  };
}

/** WP6 run 对比 */
export interface TrainingRunCompareRow {
  run_id: string;
  status?: string;
  display_name?: string;
  model_type?: string;
  target_horizon_days?: number;
  feature_count?: number;
  seed?: number;
  test_rank_ic?: number;
  strict: {
    status: string;
    segment?: string;
    window?: { start?: string; end?: string } | null;
    batches?: number;
    total_25bp?: number;
    mdd?: number;
    excess_hs300?: number;
    hac_t?: number;
    psr?: number;
    turnover_per_batch?: number;
    error?: string;
  };
  diff_vs_baseline?: Array<{ path: string; baseline: unknown; candidate: unknown }>;
  diff_axes?: string[];
  single_axis?: boolean;
}

export interface TrainingRunCompareResult {
  baseline_run_id: string;
  runs: TrainingRunCompareRow[];
}

/** WP7 数据审计 */
export interface TrainingAuditCheck {
  id: string;
  level: 'fail' | 'warn' | 'info' | string;
  status: 'pass' | 'fail' | string;
  detail: string;
}

export interface TrainingAuditRow {
  run_id: string;
  model_id?: string | null;
  status: 'ready' | 'running' | 'failed' | string;
  blocked: boolean;
  error?: string;
  generated_at?: string;
  report: {
    version: number;
    run_id: string;
    blocked: boolean;
    warnings: string[];
    checks: TrainingAuditCheck[];
  };
}

/** 训练数据缓存探测结果（D3；key 与训练侧同源） */
export interface TrainingCacheStatus {
  hit: boolean;
  key: string;
  cache_dir: string;
  bytes?: number;
  rows?: number;
  features?: string[];
  created_at?: string;
  factor_source?: string;
  train_start?: string;
  train_end?: string;
  valid_end?: string;
  test_end?: string;
  horizon?: number;
  target_mode?: string;
  label_transform?: string;
  label_top_pct?: number;
  execution_price?: string;
}

/** 任务提交时的请求载荷（与轮询端点分开：载荷含全部特征，体积大，只拉一次） */
export interface ModelTrainingRunRequest {
  runId: string;
  request: Record<string, unknown>;
}

/** 训练记录列表的一行（/models/training-runs 摘要） */
export interface TrainingRunHistoryItem {
  run_id: string;
  status: string;
  progress: number;
  created_at: string | null;
  updated_at: string | null;
  duration_seconds: number | null;
  display_name: string;
  model_type: string;
  market: string;
  target_horizon_days: number | null;
  feature_count: number;
  model_id: string;
  metrics: {
    test_rank_ic: number | null;
    test_icir: number | null;
    test_auc: number | null;
    test_rmse: number | null;
    val_rank_ic: number | null;
    val_icir: number | null;
  };
  strict: {
    status?: string;
    segment?: string;
    batches?: number | null;
    total_25bp?: number | null;
    mdd?: number | null;
    excess_hs300?: number | null;
    hac_t?: number | null;
    psr?: number | null;
    turnover_per_batch?: number | null;
    error?: string;
  } | null;
  has_result: boolean;
  has_logs: boolean;
  logs_chars: number;
}

export interface TrainingRunHistoryResponse {
  items: TrainingRunHistoryItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface ModelLastInference {
  /** 最近一次成功推理的基准数据日（YYYY-MM-DD）；null = 从未成功推理。 */
  last_inference_date: string | null;
  /** 对应的预测日（基准数据日 + 预测周期）。 */
  last_prediction_date: string | null;
  /** 最近一次成功推理的落库时间（ISO）。 */
  last_success_at: string | null;
  /** 最近一次运行状态（completed / failed），含失败。 */
  last_status: string | null;
  /** 最近一次运行时间（ISO，含失败）。 */
  last_run_at: string | null;
}

export interface UserModelRecord {
  tenant_id: string;
  user_id: string;
  model_id: string;
  source_run_id: string;
  status: string;
  storage_path: string;
  model_file: string;
  market?: string;
  metadata_json: Record<string, unknown>;
  metrics_json: Record<string, unknown>;
  is_default: boolean;
  created_at?: string | null;
  updated_at?: string | null;
  activated_at?: string | null;
  /**
   * 推理必需产物缺项（空数组 = 健全）。由后端 `GET /models` 附带，
   * 让治理面板在选中模型之前就能标出「点了也跑不起来」的注册项。
   * 只覆盖「够不够得着」（目录/脚本/权重/预测产物），不覆盖 schema 漂移
   * 这类语义问题——那是推理前检的结论。
   */
  asset_gaps?: string[];
  /**
   * 最近推理摘要（`GET /models` 附带）：模型管理页靠它标出「从未推理」或
   * 「最近一次运行失败」的模型，避免漏跑。无任何推理记录时为空。
   */
  last_inference?: ModelLastInference | null;
}

/** ─── 模型融合（机构级 v2，见 docs/机构级模型融合_设计方案.md） ─── */

export interface FusionMemberRow {
  model_id: string;
  display_name: string;
  model_type: string;
  market: string;
  horizon_days: number;
  ic_days: number;
  ic_mean: number | null;
  icir: number | null;
  replay_icir: number | null;
  weight: number | null;
}

/** 逐成员权重诊断（与后端 MemberWeightDiagnostic 同构）。 */
export interface FusionDiagnosticRow {
  member_id: string;
  n_days: number;
  ic_mean: number | null;
  ic_std: number | null;
  icir: number | null;
  corr_penalty: number;
  raw: number;
  weight: number;
  dropped: boolean;
  reason: string;
}

export interface FusionReplaySummaryRow {
  ic_mean: number | null;
  icir: number | null;
  n_days: number;
}

export interface FusionReplayVerdict {
  fused_icir: number;
  fused_ic_days: number;
  best_member_icir: number;
  median_member_icir: number;
  beats_best: boolean;
  beats_median: boolean;
}

export interface FusionPreviewResponse {
  status: string;
  as_of: string;
  market: string;
  horizon_days: number;
  weight_strategy: string;
  weights: Record<string, number>;
  score_norm?: ScoreNorm;
  diagnostics: FusionDiagnosticRow[];
  members: FusionMemberRow[];
  corr: Record<string, Record<string, number>>;
  corr_warnings: { a: string; b: string; corr: number }[];
  replay: {
    dates: number;
    summary: Record<string, FusionReplaySummaryRow>;
    verdict: FusionReplayVerdict | null;
  } | null;
  warnings: string[];
}

export type ScoreNorm = 'rank_pct' | 'zscore' | 'zscore_clip' | 'minmax';

export interface EnsembleCreatePayload {
  source_model_ids: string[];
  display_name?: string;
  weight_strategy?: 'icir_shrunk' | 'equal' | 'manual' | 'recent_ic';
  manual_weights?: Record<string, number>;
  fusion_strategy?: 'linear' | 'majority_vote' | 'periodic_hierarchy' | 'confidence_gate';
  strategy_config?: Record<string, number>;
  score_norm?: ScoreNorm;
}

export interface FusionCreateResult {
  model_id: string;
  status: string;
  preview: FusionPreviewResponse;
}

export interface ModelShapSummaryItem {
  rank: number;
  feature: string;
  mean_abs_shap: number;
  mean_shap: number;
  positive_ratio: number;
}

export interface ModelShapSummaryResponse {
  model_id: string;
  status: string;
  split?: string;
  rows_requested: number;
  rows_used: number;
  file: string;
  file_exists: boolean;
  error?: string;
  total: number;
  items: ModelShapSummaryItem[];
}

// ─── 推理相关类型 ────────────────────────────────────────────────────────────

export interface InferenceRunRecord {
  run_id: string;
  model_id: string;
  inference_date: string;   // 基准日期（数据日期）
  target_date: string;      // 预测目标日期
  status: 'running' | 'completed' | 'failed';
  signals_count: number;
  duration_ms: number;
  error_msg?: string;
  created_at: string;
  updated_at?: string;
  requested_inference_date?: string;
  calendar_adjusted?: boolean;
  data_trade_date?: string;
  prediction_trade_date?: string;
  fallback_used?: boolean;
  fallback_reason?: string;
  execution_mode?: string;
  model_switch_used?: boolean;
  model_switch_reason?: string;
  failure_stage?: string;
  active_model_id?: string;
  effective_model_id?: string;
  model_source?: string;
  active_data_source?: string;
  stdout?: string;
  stderr?: string;
  error_message?: string;
  request_json?: Record<string, unknown>;
  result_json?: Record<string, unknown>;
  score_distribution?: ScoreDistribution;
  board_top1?: BoardTop1Stat[];
  board_top1_avg?: number | null;
  industry_top1?: IndustryTop1Stat[];
  industry_top1_count?: number;
  industry_avg_top1?: number | null;
  strong_industry_count?: number;
  market_signal?: MarketSignalInfo;
  score_buckets?: ScoreBucketStat[];
  gold_zone_count?: number;
  fake_signal_count?: number;
  fake_signal_symbols?: string[];
  trend_stats?: TrendStats;
  market_ma_filter?: MarketMAFilter;
  negative_analysis?: NegativeAnalysis;
  is_wide_scale?: boolean;
  /** 推理中心「排除黑名单」口径的统计（未启用时不带或 applied=false） */
  blacklist?: {
    applied: boolean;
    removed?: number;
    excluded?: number;
    asof?: string | null;
  };
}

export interface ScoreDistributionBucket {
  x0: number;
  x1: number;
  count: number;
}

export interface ScoreDistribution {
  count: number;
  positive_count: number;
  positive_pct: number;
  negative_count: number;
  negative_pct: number;
  zero_count: number;
  zero_pct: number;
  mean: number;
  median: number;
  stdev: number;
  p10: number;
  p25: number;
  p75: number;
  p90: number;
  min: number;
  max: number;
  histogram: ScoreDistributionBucket[];
}

export interface InferencePrecheckItem {
  key: string;
  label: string;
  passed: boolean;
  detail: string;
  severity?: 'hard' | 'soft';
}

export interface InferencePrecheckResult {
  passed: boolean;
  checked_at: string;
  model_id: string;
  effective_model_id: string;
  model_source: string;
  storage_path: string;
  model_file?: string;
  requested_inference_date?: string;
  calendar_adjusted?: boolean;
  data_trade_date: string;
  prediction_trade_date: string;
  items: InferencePrecheckItem[];
}

export interface InferenceExecutionResult extends InferenceRunRecord {
  success: boolean;
  precheck?: InferencePrecheckResult;
}

export interface InferenceRankingItem {
  rank: number;
  code: string;
  name: string;
  score: number;
  signal: 'buy' | 'sell' | 'hold';
  board?: string;
  industry?: string;
  trend?: string;
  prev_score?: number | null;
  prev2_score?: number | null;
  next_score?: number | null;
  market_cap_yi?: number | null;
  market_cap_tier?: string;
  negative_tag?: string;
  /** 中性化分位（影子层输出，0~1；null=该标的不可用） */
  neutral_score?: number | null;
  /** 中性化口径下的名次（按 neutral_score 降序，仅有效标的参与） */
  neutral_rank?: number | null;
  /** 名次变化 = 原始 rank − 中性化 rank；正数表示中性化后上升 */
  neutral_delta?: number | null;
  /** 信号价（推理时刻的参考价；缺失为 null，绝不补 0） */
  expected_price?: number | null;
  /** 信号价来源：live=盘中实时快照；prev_close=盘前无快照时的上一交易日收盘 */
  expected_price_source?: 'live' | 'prev_close' | null;
}

export interface NegativeCandidate {
  symbol: string;
  name: string;
  score: number;
  tier: string;
  industry: string;
}

export interface StockScoreHistoryItem {
  trade_date: string;
  data_trade_date?: string;
  fusion_score: number | null;
  signal_side: string | null;
  score_rank: number | null;
  run_id: string;
  created_at: string | null;
  signal_model_id?: string;
  /** T+N 目标日（后端按交易日历算好；非交易日/日历越界为 null） */
  target_date?: string | null;
}

/** 模型下拉/曲线元数据（与后端 score-series.models 对齐） */
export interface StockScoreModelMeta {
  model_id: string;
  display_name?: string;
  is_default?: boolean;
  train_start?: string;
  train_end?: string;
  /** 模型真实训练周期 T+N；老模型未记录时为 null */
  horizon?: number | null;
  target_mode?: string | null;
  execution_lag_days?: number;
  /** 是否有 pred.parquet 产物；false = 选中也画不出分数 */
  has_pred?: boolean;
  /** pred.parquet 的文件日期（快速新鲜度近似；精确最后分数日看 chosen model） */
  pred_mtime?: string | null;
  /** 选中模型的精确最后分数日；仅 model 字段保证有 */
  pred_last_date?: string | null;
  asset_gaps?: string[];
}

export interface StockScoreWindow {
  data_date: string;
  entry_date: string;
  target_date: string;
  horizon: number;
  execution_lag_days: number;
  /** 执行日 → 目标日的全部交易日（含两端） */
  window_dates: string[];
}

export interface StockScoreHistoryResponse {
  symbol: string;
  normalized_symbol: string;
  name: string;
  industry: string;
  board?: string;
  total: number;
  items: StockScoreHistoryItem[];
  models?: StockScoreModelMeta[];
  /** 当前分数序列对应的模型（历史/产物/周期/新鲜度） */
  model?: StockScoreModelMeta | null;
  /** 以最后一条真实分数日为锚点的 T+N 窗口 */
  window?: StockScoreWindow | null;
  window_warning?: string | null;
  last_score_date?: string | null;
  /** 最后分数日距今的自然日数；>0 表示分数过期，前端必须显性提示 */
  stale_days?: number | null;
  score_source?: 'pred_parquet' | 'inference_runs';
  source?: 'pred_parquet' | 'signal_scores';
}

export interface NegativeAnalysis {
  negative_count: number;
  negative_pct: number;
  neg_buckets: Record<string, number>;
  neg_by_tier: Record<string, number>;
  short_candidates_count: number;
  short_candidates: NegativeCandidate[];
  mistake_candidates_count: number;
  mistake_candidates: NegativeCandidate[];
  extreme_neg_count: number;
  short_industries: Array<{ industry: string; count: number; avg_score: number }>;
  resistant_industries: Array<{ industry: string; count: number; avg_score: number }>;
}

export interface InferenceRankingResult {
  run_id: string;
  inference_date: string;
  target_date: string;
  model_id: string;
  summary?: InferenceRunRecord;
  rankings: InferenceRankingItem[];
}

export interface BoardTop1Stat {
  board: string;
  top1_score: number;
  top1_symbol: string;
  top1_name: string;
}

export interface IndustryTop1Stat {
  industry: string;
  top1_score: number;
  top1_symbol: string;
  top1_name: string;
}

export interface MarketSignalInfo {
  avg_top1: number | null;
  strong_industry_count: number;
  entry_signal: 'strong' | 'weak' | 'empty';
  entry_threshold: number;
  empty_threshold: number;
  strong_threshold: number;
  label: string;
  score_scale?: 'normal' | 'wide';
}

export interface ScoreBucketStat {
  key: string;
  label: string;
  action: string;
  color: string;
  count: number;
  symbols: string[];
}

export interface TrendStats {
  [trend: string]: number;
}

export interface MarketMAFilter {
  symbol: string;
  dates: string[];
  close: number | null;
  mavg: Record<string, number | null>;
  below_ma20: boolean;
  ref_date: string;
}

export interface AutoInferenceSettings {
  enabled: boolean;
  schedule_desc: string;
  schedule_time?: string;
  last_run?: InferenceRunRecord;
  next_run?: string;
  updated_at?: string;
}

/** 回测交易成本覆盖参数。留空则回退模型 metadata.context，再回退 A股标准费率。 */
export interface TradingCostParams {
  commission_rate?: number;
  stamp_duty?: number;
  transfer_fee?: number;
  slippage?: number;
}

export interface LatestInferenceRunInfo {
  latest_key: string;
  run_id: string;
  model_id: string;
  prediction_trade_date?: string;
  target_date?: string;
  status?: string;
  updated_at?: string;
  matched_model?: boolean | null;
}

export interface StrategyModelBinding {
  tenant_id: string;
  user_id: string;
  strategy_id: string;
  model_id: string;
  model_status?: string;
  storage_path?: string;
  model_file?: string;
  updated_at?: string | null;
}

export interface TradingDayCheckResult {
  market: string;
  date: string;
  is_trading_day: boolean;
  tenant_id?: string;
  user_id?: string;
}

// ─── 批量推理相关类型 ────────────────────────────────────────────────────────

export interface BatchInferenceRequest {
  model_id: string;
  mode?: 'lookback' | 'range';
  anchor_date?: string;
  start_date?: string;
  end_date?: string;
  window_days?: number;
  top_k?: number;
  side?: 'long' | 'short' | 'both';
  reuse_existing?: boolean;
  concurrency?: number;
}

export interface BatchInferenceRecord {
  batch_id: string;
  model_id: string;
  anchor_date: string;
  window_days: number;
  horizon_days: number;
  status: 'pending' | 'running' | 'completed' | 'partial' | 'failed';
  progress_done: number;
  progress_total: number;
  current_trade_date?: string;
  member_runs?: Array<{
    trade_date: string;
    run_id: string;
    status: string;
    reused: boolean;
    signals_count: number;
  }>;
  params?: {
    window_meta?: {
      mode?: 'lookback' | 'range';
      start_date?: string;
      end_date?: string;
      anchor_date?: string;
      requested_start_date?: string;
      requested_end_date?: string;
      requested_anchor_date?: string;
      trade_dates?: string[];
      warnings?: string[];
    };
    concurrency?: number;
  };
  error_message?: string;
  created_at: string;
  updated_at?: string;
}

export interface BatchAggregateSymbol {
  symbol: string;
  stock_name: string;
  appear_days: number;
  coverage: number;
  mean_pct: number;
  weighted_pct: number;
  std_pct: number;
  mean_rank: number;
  best_rank: number;
  trend_rho: number | null;
  topk_hits: number;
  band_hits: number;
  bottomk_hits: number;
  band_hits_short: number;
  is_monotonic_up: boolean;
  is_monotonic_down: boolean;
  up_streak: number;
  down_streak: number;
  conviction_long: number;
  conviction_short: number;
  membership: string;
  buy_days: number;
  sell_days: number;
}

export interface BatchAggregateMoverMetric {
  metric: string;
  description: string;
  warning: string | null;
  up: Array<Record<string, unknown>>;
  down: Array<Record<string, unknown>>;
}

export interface BatchAggregateDaily {
  trade_date: string;
  missing: boolean;
  count: number;
  score_mean?: number | null;
  score_std?: number | null;
  buy_count?: number | null;
  sell_count?: number | null;
  topk_turnover?: number | null;
  consensus_overlap?: number | null;
  [key: string]: unknown;
}

export interface BatchAggregateResult {
  batch_id: string;
  model_id: string;
  anchor_date: string;
  cached: boolean;
  meta: {
    window_days: number;
    horizon_days: number;
    effective_independent_bets: number;
    overlap_ratio: number;
    signal_autocorr: number | null;
    symbol_count: number;
    dates: string[];
    anchor_adjusted?: boolean;
    span_calendar_days?: number;
    warnings: string[];
    [key: string]: unknown;
  };
  per_symbol: BatchAggregateSymbol[];
  groups: Record<string, BatchAggregateSymbol[]>;
  movers: Record<string, BatchAggregateMoverMetric>;
  daily: BatchAggregateDaily[];
}

// ─── 系统内置模型（来自 models/production 目录）─────────────────────────────

export interface SystemModelRecord {
  model_id: string;
  dir_name: string;
  tenant_id: 'system';
  market?: string;
  display_name: string;
  description: string;
  framework: string;
  model_type: string;
  feature_count: number | null;
  feature_columns: string[];
  is_neutralized: boolean;
  algorithm: string;
  version: string;
  created_at: string;
  training_config: Record<string, unknown>;
  train_start?: string;
  train_end?: string;
  valid_start?: string;
  valid_end?: string;
  test_start?: string;
  test_end?: string;
  performance_metrics: {
    train?: { mean_ic?: number; icir?: number; annualized_return?: number; max_drawdown?: number; sharpe?: number };
    valid?: { mean_ic?: number; icir?: number; annualized_return?: number; max_drawdown?: number; sharpe?: number };
    test?: { mean_ic?: number; icir?: number; annualized_return?: number; max_drawdown?: number; sharpe?: number };
  };
  inference_config: Record<string, unknown>;
  files: Record<string, string>;
}

class ModelTrainingService {
  private client: AxiosInstance;
  private readonly baseURL = resolveWebSafeServiceBase(
    (import.meta as any).env?.VITE_USER_API_URL,
    SERVICE_ENDPOINTS.USER_SERVICE,
  );

  constructor() {
    this.client = axios.create({
      timeout: 30000,
      headers: {
        'Content-Type': 'application/json',
      },
    });

    this.client.interceptors.request.use((config) => {
      config.baseURL = resolveWebSafeServiceBase(
        (import.meta as any).env?.VITE_USER_API_URL,
        SERVICE_ENDPOINTS.USER_SERVICE,
      );
      return config;
    });

    this.client.interceptors.request.use((config) => {
      const token = authService.getAccessToken();
      if (token) {
        if (config.headers && typeof config.headers.set === 'function') {
          config.headers.set('Authorization', `Bearer ${token}`);
        } else if (config.headers) {
          config.headers.Authorization = `Bearer ${token}`;
        }
      }

      const tenantId = authService.getTenantId?.() || 'default';
      if (config.headers && typeof config.headers.set === 'function') {
        if (!config.headers.has('X-Tenant-Id') && !config.headers.has('x-tenant-id')) {
          config.headers.set('X-Tenant-Id', tenantId);
        }
      } else if (config.headers) {
        if (!config.headers['X-Tenant-Id'] && !config.headers['x-tenant-id']) {
          config.headers['X-Tenant-Id'] = tenantId;
        }
      }
      return config;
    });

    this.client.interceptors.response.use(
      (response) => response,
      async (error) => authService.handle401Error(error, this.client)
    );
  }

  async getFeatureCatalog(market?: string, includeCoverage = false, factorSource?: string): Promise<AdminModelFeatureCatalog> {
    const params: Record<string, string> = {};
    if (market) params.market = market;
    if (includeCoverage) params.include_coverage = 'true';
    if (factorSource) params.factor_source = factorSource;
    const resp = await this.client.get<AdminModelFeatureCatalog>('/models/feature-catalog', { params });
    return resp.data;
  }

  async getQuantDBTrainingSources(market?: string): Promise<QuantDBTrainingSourcesResult> {
    const resp = await this.client.get<QuantDBTrainingSourcesResult>('/models/training-sources', {
      params: market ? { market } : undefined,
    });
    return resp.data;
  }

  /** 训练目标 × 13 模型能力矩阵（后端单一事实源，只读）。 */
  async getTrainingCapabilities(): Promise<TrainingCapabilities> {
    const resp = await this.client.get<TrainingCapabilities>('/models/training/capabilities');
    return resp.data;
  }

  /** 目标注册表（只读）：前端只展示 active；legacy 目标由表单本地表达。 */
  async listLabelTargets(params?: {
    market?: string;
    status?: string;
    family?: string;
  }): Promise<LabelTargetListResponse> {
    const resp = await this.client.get<LabelTargetListResponse>('/models/training/targets', {
      params,
    });
    return resp.data;
  }

  async runTraining<T extends object>(payload: T): Promise<ModelTrainingRunResponse> {
    const resp = await this.client.post<ModelTrainingRunResponse>('/models/run-training', payload);
    return resp.data;
  }

  /** 探测本地训练数据缓存（同特征/窗口/周期换模型会命中；模型超参不影响 key） */
  async getTrainingCacheStatus<T extends object>(payload: T): Promise<TrainingCacheStatus> {
    const resp = await this.client.post<TrainingCacheStatus>(
      '/models/training-cache/status',
      payload,
    );
    return resp.data;
  }

  /** 清理指定缓存（管理员）；用于数据更新后强制重建 */
  async deleteTrainingCache(key: string): Promise<{ deleted: boolean; key: string }> {
    const resp = await this.client.delete<{ deleted: boolean; key: string }>(
      `/models/training-cache/${key}`,
    );
    return resp.data;
  }

  /** 训练记录列表（训练历史）：摘要 + 分页；详情/日志走 getTrainingRun。 */
  async listTrainingRuns(params: {
    status?: string;
    market?: string;
    q?: string;
    limit?: number;
    offset?: number;
  } = {}): Promise<TrainingRunHistoryResponse> {
    const resp = await this.client.get<TrainingRunHistoryResponse>('/models/training-runs', {
      params: {
        ...(params.status ? { status: params.status } : {}),
        ...(params.market ? { market: params.market } : {}),
        ...(params.q ? { q: params.q } : {}),
        limit: params.limit ?? 50,
        offset: params.offset ?? 0,
      },
    });
    return resp.data;
  }

  async getTrainingRun(runId: string): Promise<ModelTrainingRunStatus> {
    const resp = await this.client.get<ModelTrainingRunStatus>(`/models/training-runs/${runId}`);
    return resp.data;
  }

  /** 模型列表偏好（置顶/收藏/排序/文件夹）：服务端持久化，重启/换浏览器不丢。 */
  async getModelListPrefs(market: string): Promise<{
    market: string;
    pinned: string[];
    favorites: string[];
    sort_key: string;
    folders: Array<{ id: string; name: string }>;
    model_folders: Record<string, string>;
  }> {
    const resp = await this.client.get('/models/list-prefs', { params: { market } });
    return resp.data;
  }

  async saveModelListPrefs(payload: {
    market: string;
    pinned: string[];
    favorites: string[];
    sort_key: string;
    folders: Array<{ id: string; name: string }>;
    model_folders: Record<string, string>;
  }): Promise<{ saved: boolean }> {
    const resp = await this.client.put('/models/list-prefs', payload);
    return resp.data;
  }

  /** 严格评估报告（WP2）：未生成/计算中返回 null，由页面显示占位。 */
  async getTrainingStrictReport(runId: string): Promise<StrictEvalReportRow | null> {
    try {
      const resp = await this.client.get<StrictEvalReportRow>(
        `/models/training-runs/${runId}/strict-report`,
      );
      return resp.data ?? null;
    } catch {
      return null;
    }
  }

  /** WP6 训练 run 对比：同口径严格评估 + 配置 diff + 单轴判定。 */
  async compareTrainingRuns(ids: string[]): Promise<TrainingRunCompareResult | null> {
    try {
      const resp = await this.client.get<TrainingRunCompareResult>(
        `/models/training-runs/compare`,
        { params: { ids: ids.join(',') } },
      );
      return resp.data ?? null;
    } catch {
      return null;
    }
  }

  /** WP7 数据审计报告：未生成返回 null。 */
  async getTrainingAuditReport(runId: string): Promise<TrainingAuditRow | null> {
    try {
      const resp = await this.client.get<TrainingAuditRow>(
        `/models/training-runs/${runId}/audit-report`,
      );
      return resp.data ?? null;
    } catch {
      return null;
    }
  }

  /**
   * 重算严格评估（管理员，后台异步）。
   * 老模型没有严格评估时由结果页按钮触发；非管理员会收到 403。
   */
  async recomputeTrainingStrictReport(runId: string): Promise<{ ok: boolean; scheduled: boolean }> {
    const resp = await this.client.post<{ ok: boolean; scheduled: boolean }>(
      `/admin/models/training-runs/${runId}/strict-report`,
    );
    return resp.data;
  }

  /** 重算数据审计（管理员，后台异步）。老模型由结果页按钮触发。 */
  async recomputeTrainingAuditReport(runId: string): Promise<{ ok: boolean; scheduled: boolean }> {
    const resp = await this.client.post<{ ok: boolean; scheduled: boolean }>(
      `/admin/models/training-runs/${runId}/audit-report`,
    );
    return resp.data;
  }

  /**
   * 拉取该任务**提交时**的请求载荷，供恢复监控已有任务时展示真实的请求预览。
   * 拉取失败（任务不存在/无权）返回 null，由页面回落到页内草稿预览。
   */
  async getTrainingRunRequest(runId: string): Promise<ModelTrainingRunRequest | null> {
    try {
      const resp = await this.client.get<ModelTrainingRunRequest>(`/models/training-runs/${runId}/request`);
      return resp.data ?? null;
    } catch {
      return null;
    }
  }

  async cancelTrainingRun(runId: string): Promise<{ runId: string; status: string; cancelled: boolean }> {
    const resp = await this.client.post<{ runId: string; status: string; cancelled: boolean }>(
      `/models/training-runs/${runId}/cancel`,
    );
    return resp.data;
  }

  async getActiveTrainingRun(): Promise<ModelTrainingRunStatus | null> {
    // 后端对「无活跃训练任务」返回 200 + null（不再 404），此处兜底网络/未知错误
    try {
      const resp = await this.client.get<ModelTrainingRunStatus | null>(`/models/training-runs/active`);
      return resp.data ?? null;
    } catch {
      return null; // 无记录视为无活跃训练
    }
  }

  async listUserModels(includeArchived = false, market?: string): Promise<{ items: UserModelRecord[]; total: number }> {
    const resp = await this.client.get<{ items: UserModelRecord[]; total: number }>(`/models`, {
      params: { include_archived: includeArchived, ...(market ? { market } : {}) },
    });
    return resp.data;
  }

  async listSystemModels(market?: string): Promise<SystemModelRecord[]> {
    try {
      const resp = await this.client.get<{ status: string; count: number; models: SystemModelRecord[] }>(
        `/models/system-models`,
        { params: market ? { market } : undefined },
      );
      return resp.data.models ?? [];
    } catch {
      return [];
    }
  }

  async getDefaultModel(market?: string): Promise<UserModelRecord> {
    const resp = await this.client.get<UserModelRecord>(`/models/default`, {
      params: market ? { market } : undefined,
    });
    return resp.data;
  }

  async setDefaultModel(modelId: string, force = false): Promise<UserModelRecord> {
    const resp = await this.client.patch<UserModelRecord>(`/models/default`, {
      model_id: modelId,
      force,
    });
    return resp.data;
  }

  async getUserModel(modelId: string): Promise<UserModelRecord> {
    const resp = await this.client.get<UserModelRecord>(`/models/${modelId}`);
    return resp.data;
  }

  async getModelShapSummary(modelId: string): Promise<ModelShapSummaryResponse> {
    const resp = await this.client.get<ModelShapSummaryResponse>(`/models/${encodeURIComponent(modelId)}/shap-summary`);
    const data = resp.data as any;
    const rawItems = Array.isArray(data?.items) ? data.items : [];
    return {
      model_id: String(data?.model_id ?? modelId),
      status: String(data?.status ?? 'missing'),
      split: data?.split ? String(data.split) : undefined,
      rows_requested: Number(data?.rows_requested ?? 0),
      rows_used: Number(data?.rows_used ?? 0),
      file: String(data?.file ?? ''),
      file_exists: Boolean(data?.file_exists),
      error: data?.error ? String(data.error) : undefined,
      total: Number(data?.total ?? rawItems.length ?? 0),
      items: rawItems.map((item: any, idx: number) => ({
        rank: Number(item?.rank ?? idx + 1),
        feature: String(item?.feature ?? ''),
        mean_abs_shap: Number(item?.mean_abs_shap ?? 0),
        mean_shap: Number(item?.mean_shap ?? 0),
        positive_ratio: Number(item?.positive_ratio ?? 0),
      })),
    };
  }

  async archiveUserModel(modelId: string): Promise<UserModelRecord> {
    const resp = await this.client.post<UserModelRecord>(`/models/${modelId}/archive`);
    return resp.data;
  }

  /** 已归档 → 恢复使用（ready/candidate）；不自动接管默认位。 */
  async restoreUserModel(modelId: string): Promise<UserModelRecord> {
    const resp = await this.client.post<UserModelRecord>(`/models/${encodeURIComponent(modelId)}/restore`);
    return resp.data;
  }

  async activateUserModel(modelId: string, force = false): Promise<UserModelRecord> {
    const resp = await this.client.post<UserModelRecord>(
      `/models/${modelId}/activate`,
      undefined,
      { params: force ? { force: true } : undefined },
    );
    return resp.data;
  }

  async renameUserModel(modelId: string, displayName: string): Promise<UserModelRecord> {
    const resp = await this.client.patch<UserModelRecord>(
      `/models/${encodeURIComponent(modelId)}/display-name`,
      { display_name: displayName },
    );
    return resp.data;
  }

  /** 融合预览（无副作用）：成员证据 / 相关性 / 权重诊断 / OOS 回放。 */
  async previewEnsemble(payload: {
    source_model_ids: string[];
    weight_strategy?: string;
    manual_weights?: Record<string, number>;
    score_norm?: ScoreNorm;
  }): Promise<FusionPreviewResponse> {
    const resp = await this.client.post<FusionPreviewResponse>(
      `/models/ensemble/preview`,
      payload,
    );
    return resp.data;
  }

  /** 创建融合模型；创建即进入日更推理名单，权重由服务器端权威计算。 */
  async createEnsemble(payload: EnsembleCreatePayload): Promise<FusionCreateResult> {
    const resp = await this.client.post<FusionCreateResult>(
      `/models/ensemble/create`,
      payload,
    );
    return resp.data;
  }

  async getModelDrift(modelId: string): Promise<any> {
    const resp = await this.client.get<any>(`/models/${encodeURIComponent(modelId)}/drift`);
    return resp.data;
  }

  /** 入模因子的原始库来源（合并库 provenance，列级）。 */
  async getModelFactorProvenance(modelId: string): Promise<{
    model_id: string;
    market: string;
    dataset_id: string;
    total: number;
    resolved: number;
    library_counts: Record<string, number>;
    items: Array<{
      feature: string;
      source_market: string;
      source_dataset: string;
      source_column: string;
      candidates?: Array<{ market: string; dataset_id: string }>;
    }>;
    unresolved: string[];
  }> {
    const resp = await this.client.get(`/models/${encodeURIComponent(modelId)}/factor-provenance`);
    return resp.data;
  }

  async getMarketRegime(modelId: string, window = 90, indexOffset = 1): Promise<any> {
    const resp = await this.client.get<any>(`/models/${encodeURIComponent(modelId)}/market-regime`, {
      params: { window, index_offset: indexOffset },
    });
    return resp.data;
  }

  async getInferenceCoverage(modelId: string): Promise<any> {
    const resp = await this.client.get<any>(`/models/${encodeURIComponent(modelId)}/inference/coverage`);
    return resp.data;
  }

  /** 模型管理「重新训练」：读取模型训练配置（config.yaml 优先，metadata 兜底）。 */
  async getRetrainConfig(modelId: string): Promise<RetrainConfigPayload> {
    const resp = await this.client.get<RetrainConfigPayload>(
      `/models/${encodeURIComponent(modelId)}/retrain-config`,
    );
    return resp.data;
  }

  async triggerInferenceBackfill(modelId: string): Promise<any> {
    const resp = await this.client.post<any>(`/models/${encodeURIComponent(modelId)}/inference/backfill`);
    return resp.data;
  }

  async getBackfillStatus(modelId: string, taskId: string): Promise<any> {
    const resp = await this.client.get<any>(`/models/${encodeURIComponent(modelId)}/inference/backfill/${encodeURIComponent(taskId)}`);
    return resp.data;
  }

  // ── 多模型串行补推（/models/inference/backfill-jobs） ──────────────

  async previewBackfillJobs(modelIds: string[]): Promise<BackfillPreview> {
    const resp = await this.client.post<BackfillPreview>(
      '/models/inference/backfill-jobs/preview',
      { model_ids: modelIds },
    );
    return resp.data;
  }

  async createBackfillJob(modelIds: string[], mode: 'gaps' | 'range' = 'gaps'): Promise<BackfillJob> {
    const resp = await this.client.post<BackfillJob>('/models/inference/backfill-jobs', {
      model_ids: modelIds,
      mode,
    });
    return resp.data;
  }

  async listBackfillJobs(limit = 20): Promise<BackfillJobList> {
    const resp = await this.client.get<BackfillJobList>('/models/inference/backfill-jobs', {
      params: { limit },
    });
    return resp.data;
  }

  async getBackfillJob(jobId: string): Promise<BackfillJob> {
    const resp = await this.client.get<BackfillJob>(
      `/models/inference/backfill-jobs/${encodeURIComponent(jobId)}`,
    );
    return resp.data;
  }

  async cancelBackfillJob(jobId: string): Promise<BackfillJob> {
    const resp = await this.client.post<BackfillJob>(
      `/models/inference/backfill-jobs/${encodeURIComponent(jobId)}/cancel`,
    );
    return resp.data;
  }

  async batchCheckTradingDays(market: string, dates: string[]): Promise<Record<string, boolean>> {
    const resp = await this.client.post<{ results: Array<{ date: string; is_trading_day: boolean }> }>('/market-calendar/batch-check', { market, dates });
    const map: Record<string, boolean> = {};
    for (const r of resp.data.results || []) {
      map[String(r.date).slice(0, 10)] = Boolean((r as any).is_trading_day);
    }
    return map;
  }

  async getModelQuality(modelId: string, window = 60): Promise<any> {
    const resp = await this.client.get<any>(`/models/${modelId}/quality`, { params: { window } });
    return resp.data;
  }

  /**
   * 测试集实盘力（滚动 3 个月）：缓存/运行状态。
   * `excludeBlacklist=true` 走「TopN 剔除黑名单」口径（独立缓存，不覆盖默认视图）。
   */
  async getModelSignalEval(modelId: string, options?: { excludeBlacklist?: boolean }): Promise<SignalEvalState> {
    const resp = await this.client.get<SignalEvalState>(
      `/models/${encodeURIComponent(modelId)}/signal-eval`,
      { params: options?.excludeBlacklist ? { exclude_blacklist: true } : undefined },
    );
    return resp.data;
  }

  /** 触发后台重算（幂等：已有作业时返回 running）。 */
  async refreshModelSignalEval(
    modelId: string,
    options?: { excludeBlacklist?: boolean },
  ): Promise<{ status: string }> {
    const resp = await this.client.post<{ status: string }>(
      `/models/${encodeURIComponent(modelId)}/signal-eval/refresh`,
      undefined,
      { params: options?.excludeBlacklist ? { exclude_blacklist: true } : undefined },
    );
    return resp.data;
  }

  async compareModels(modelIds: string[]): Promise<any> {
    const resp = await this.client.post<any>('/models/compare', { model_ids: modelIds });
    return resp.data;
  }

  async getStrategyBinding(strategyId: string): Promise<StrategyModelBinding> {
    const resp = await this.client.get<StrategyModelBinding>(`/models/strategy-bindings/${strategyId}`);
    return resp.data;
  }

  async setStrategyBinding(strategyId: string, modelId: string, force = false): Promise<StrategyModelBinding> {
    const resp = await this.client.put<StrategyModelBinding>(`/models/strategy-bindings/${strategyId}`, {
      model_id: modelId,
      force,
    });
    return resp.data;
  }

  async deleteStrategyBinding(strategyId: string): Promise<{ deleted: boolean; strategy_id: string }> {
    const resp = await this.client.delete<{ deleted: boolean; strategy_id: string }>(
      `/models/strategy-bindings/${strategyId}`,
    );
    return resp.data;
  }

  async precheckInference(modelId: string, inferenceDate?: string): Promise<InferencePrecheckResult> {
    const resp = await this.client.get<InferencePrecheckResult>('/models/inference/precheck', {
      params: {
        model_id: modelId,
        inference_date: inferenceDate,
      },
    });
    return resp.data;
  }

  async checkTradingDay(market: string, date: string): Promise<TradingDayCheckResult> {
    const resp = await this.client.get<TradingDayCheckResult>('/market-calendar/is-trading-day', {
      params: { market, date },
    });
    return resp.data;
  }

  async nextTradingDay(market: string, date: string): Promise<string> {
    const resp = await this.client.get<{ next_trading_day: string }>('/market-calendar/next-trading-day', {
      params: { market, date },
    });
    return String(resp.data?.next_trading_day || '');
  }

  async prevTradingDay(market: string, date: string): Promise<string> {
    const resp = await this.client.get<{ prev_trading_day: string }>('/market-calendar/prev-trading-day', {
      params: { market, date },
    });
    return String(resp.data?.prev_trading_day || '');
  }

  async resolveInferenceDateByCalendar(market: string, date: string): Promise<{ date: string; adjusted: boolean }> {
    const check = await this.checkTradingDay(market, date);
    if (check.is_trading_day) {
      return { date, adjusted: false };
    }
    const prev = await this.prevTradingDay(market, date);
    return { date: prev, adjusted: true };
  }

  async calcTargetDateByCalendar(market: string, baseDate: string, horizonDays: number): Promise<string> {
    let cursor = baseDate;
    const steps = Math.max(1, Number(horizonDays || 1));
    for (let i = 0; i < steps; i += 1) {
      cursor = await this.nextTradingDay(market, cursor);
    }
    return cursor;
  }

  private normalizeInferenceRun(raw: any): InferenceRunRecord {
    return {
      run_id: String(raw?.run_id ?? ''),
      model_id: String(raw?.model_id ?? ''),
      inference_date: String(raw?.inference_date ?? raw?.data_trade_date ?? ''),
      target_date: String(raw?.target_date ?? raw?.prediction_trade_date ?? ''),
      status: (raw?.status ?? 'failed') as 'running' | 'completed' | 'failed',
      signals_count: Number(raw?.signals_count ?? 0),
      duration_ms: Number(raw?.duration_ms ?? 0),
      error_msg: raw?.error_msg ?? raw?.error_message ?? undefined,
      created_at: String(raw?.created_at ?? ''),
      updated_at: raw?.updated_at ? String(raw.updated_at) : undefined,
      requested_inference_date: raw?.requested_inference_date ? String(raw.requested_inference_date) : undefined,
      calendar_adjusted: typeof raw?.calendar_adjusted === 'boolean' ? raw.calendar_adjusted : undefined,
      data_trade_date: raw?.data_trade_date ? String(raw.data_trade_date) : undefined,
      prediction_trade_date: raw?.prediction_trade_date ? String(raw.prediction_trade_date) : undefined,
      fallback_used: typeof raw?.fallback_used === 'boolean' ? raw.fallback_used : undefined,
      fallback_reason: raw?.fallback_reason ? String(raw.fallback_reason) : undefined,
      execution_mode: raw?.execution_mode ? String(raw.execution_mode) : (raw?.result_json?.execution_mode ? String(raw.result_json.execution_mode) : undefined),
      model_switch_used: typeof raw?.model_switch_used === 'boolean' ? raw.model_switch_used : (typeof raw?.result_json?.model_switch_used === 'boolean' ? raw.result_json.model_switch_used : undefined),
      model_switch_reason: raw?.model_switch_reason ? String(raw.model_switch_reason) : (raw?.result_json?.model_switch_reason ? String(raw.result_json.model_switch_reason) : undefined),
      failure_stage: raw?.failure_stage ? String(raw.failure_stage) : undefined,
      active_model_id: raw?.active_model_id ? String(raw.active_model_id) : undefined,
      effective_model_id: raw?.effective_model_id ? String(raw.effective_model_id) : undefined,
      model_source: raw?.model_source ? String(raw.model_source) : undefined,
      active_data_source: raw?.active_data_source ? String(raw.active_data_source) : undefined,
      stdout: raw?.stdout ? String(raw.stdout) : undefined,
      stderr: raw?.stderr ? String(raw.stderr) : undefined,
      error_message: raw?.error_message ? String(raw.error_message) : undefined,
      request_json: raw?.request_json && typeof raw.request_json === 'object' ? raw.request_json : undefined,
      result_json: raw?.result_json && typeof raw.result_json === 'object' ? raw.result_json : undefined,
      score_distribution: raw?.score_distribution && typeof raw.score_distribution === 'object' ? raw.score_distribution as ScoreDistribution : undefined,
      board_top1: Array.isArray(raw?.board_top1) ? raw.board_top1 as BoardTop1Stat[] : undefined,
      board_top1_avg: raw?.board_top1_avg !== undefined && raw.board_top1_avg !== null ? Number(raw.board_top1_avg) : undefined,
      industry_top1: Array.isArray(raw?.industry_top1) ? raw.industry_top1 as IndustryTop1Stat[] : undefined,
      industry_top1_count: raw?.industry_top1_count !== undefined ? Number(raw.industry_top1_count) : undefined,
      industry_avg_top1: raw?.industry_avg_top1 !== undefined && raw.industry_avg_top1 !== null ? Number(raw.industry_avg_top1) : undefined,
      strong_industry_count: raw?.strong_industry_count !== undefined ? Number(raw.strong_industry_count) : undefined,
      market_signal: raw?.market_signal && typeof raw.market_signal === 'object' ? raw.market_signal as MarketSignalInfo : undefined,
      score_buckets: Array.isArray(raw?.score_buckets) ? raw.score_buckets as ScoreBucketStat[] : undefined,
      gold_zone_count: raw?.gold_zone_count !== undefined ? Number(raw.gold_zone_count) : undefined,
      fake_signal_count: raw?.fake_signal_count !== undefined ? Number(raw.fake_signal_count) : undefined,
      fake_signal_symbols: Array.isArray(raw?.fake_signal_symbols) ? raw.fake_signal_symbols as string[] : undefined,
      trend_stats: raw?.trend_stats && typeof raw.trend_stats === 'object' ? raw.trend_stats as TrendStats : undefined,
      market_ma_filter: raw?.market_ma_filter && typeof raw.market_ma_filter === 'object' ? raw.market_ma_filter as MarketMAFilter : undefined,
      negative_analysis: raw?.negative_analysis && typeof raw.negative_analysis === 'object' ? raw.negative_analysis as NegativeAnalysis : undefined,
      is_wide_scale: raw?.is_wide_scale !== undefined ? Boolean(raw.is_wide_scale) : undefined,
      blacklist: raw?.blacklist && typeof raw.blacklist === 'object' ? raw.blacklist : undefined,
    };
  }

  async runModelInference(modelId: string, inferenceDate: string, poolId?: string): Promise<InferenceExecutionResult> {
    const resp = await this.client.post<InferenceExecutionResult>('/models/inference/run', {
      model_id: modelId,
      inference_date: inferenceDate,
      // 单日推理股票池（P3）：pool:<code> 引用，后端严格裁剪信号，不进 pred.parquet
      ...(poolId?.trim() ? { pool_id: poolId.trim() } : {}),
    }, { timeout: 300000 }); // 推理可能需要较长时间，5分钟超时
    const data = resp.data as any;
    const normalized = this.normalizeInferenceRun(data);
    return {
      ...normalized,
      success: Boolean(data?.success),
      precheck: data?.precheck,
    };
  }

  async listInferenceHistory(
    modelId: string,
    options?: {
      runId?: string;
      status?: string;
      inferenceDate?: string;
      page?: number;
      pageSize?: number;
    },
  ): Promise<{ items: InferenceRunRecord[]; page: number; page_size: number; total: number }> {
    const resp = await this.client.get<{ items: InferenceRunRecord[]; page: number; page_size: number; total: number }>(
      '/models/inference/runs',
      {
        params: {
          model_id: modelId,
          run_id: options?.runId,
          status: options?.status,
          inference_date: options?.inferenceDate,
          page: options?.page ?? 1,
          page_size: options?.pageSize ?? 20,
        },
      },
    );
    return {
      ...resp.data,
      items: (resp.data.items ?? []).map((item: any) => this.normalizeInferenceRun(item)),
    };
  }

  async getInferenceResult(
    runId: string,
    limit?: number,
    options?: { excludeBlacklist?: boolean },
  ): Promise<InferenceRankingResult> {
    const resp = await this.client.get<{
      summary: InferenceRunRecord;
      page: number;
      page_size: number;
      total: number;
      items: Array<{
        symbol: string;
        fusion_score: number | null;
        light_score: number | null;
        tft_score: number | null;
        score_rank: number | null;
        signal_side: string | null;
        expected_price: number | null;
        expected_price_source: string | null;
        quality: string | null;
        created_at: string | null;
        stock_name?: string | null;
        board?: string | null;
        industry?: string | null;
        trend?: string | null;
        prev_score?: number | null;
        prev2_score?: number | null;
        next_score?: number | null;
        market_cap_yi?: number | null;
        market_cap_tier?: string | null;
        negative_tag?: string | null;
      }>;
    }>(`/models/inference/runs/${runId}`, {
      params: {
        ...(limit && limit > 0 ? { limit } : {}),
        ...(options?.excludeBlacklist ? { exclude_blacklist: true } : {}),
      },
    });

    const data = resp.data;
    const summary = this.normalizeInferenceRun(data.summary);
    return {
      run_id: summary.run_id,
      inference_date: summary.inference_date,
      target_date: summary.target_date,
      model_id: summary.model_id,
      summary,
      rankings: data.items.map((item, index) => ({
        rank: item.score_rank ?? index + 1,
        code: item.symbol,
        name: item.stock_name || item.symbol,
        score: Number(item.fusion_score ?? 0),
        signal: (item.signal_side || '').toUpperCase() === 'BUY' ? 'buy' : (item.signal_side || '').toUpperCase() === 'SELL' ? 'sell' : 'hold',
        board: item.board || undefined,
        industry: item.industry || undefined,
        trend: item.trend || undefined,
        prev_score: item.prev_score !== null && item.prev_score !== undefined ? Number(item.prev_score) : null,
        prev2_score: item.prev2_score !== null && item.prev2_score !== undefined ? Number(item.prev2_score) : null,
        next_score: item.next_score !== null && item.next_score !== undefined ? Number(item.next_score) : null,
        market_cap_yi: item.market_cap_yi !== null && item.market_cap_yi !== undefined ? Number(item.market_cap_yi) : null,
        market_cap_tier: item.market_cap_tier || undefined,
        negative_tag: item.negative_tag || undefined,
        expected_price:
          item.expected_price !== null && item.expected_price !== undefined
            ? Number(item.expected_price)
            : null,
        expected_price_source:
          item.expected_price_source === 'live' || item.expected_price_source === 'prev_close'
            ? item.expected_price_source
            : null,
      })),
    };
  }

  async getStockInferenceHistory(
    symbol: string,
    days = 180,
    modelId?: string,
    endDate?: string,
    market?: string,
    includeModels = true,
  ): Promise<StockScoreHistoryResponse> {
    const resp = await this.client.get<StockScoreHistoryResponse>(
      `/models/inference/stock/${encodeURIComponent(symbol)}/history`,
      {
        params: {
          days,
          ...(modelId ? { model_id: modelId } : {}),
          ...(endDate ? { end_date: endDate } : {}),
          ...(market ? { market } : {}),
          ...(includeModels ? {} : { include_models: false }),
        },
      },
    );
    return resp.data;
  }

  /**
   * 语义化分数序列接口：与 history 同源，额外保证返回 model/window/last_score_date/stale_days。
   * 单票研判必须带 endDate（防前视）；个股终端不传 endDate（看最新）。
   */
  async getStockScoreSeries(opts: {
    symbol: string;
    days?: number;
    modelId?: string;
    endDate?: string;
    market?: string;
    includeRank?: boolean;
  }): Promise<StockScoreHistoryResponse> {
    const params = {
      days: opts.days ?? 180,
      ...(opts.modelId ? { model_id: opts.modelId } : {}),
      ...(opts.endDate ? { end_date: opts.endDate } : {}),
      ...(opts.market ? { market: opts.market } : {}),
      ...(opts.includeRank ? { include_rank: true } : {}),
    };
    try {
      const resp = await this.client.get<StockScoreHistoryResponse>(
        `/models/inference/stock/${encodeURIComponent(opts.symbol)}/score-series`,
        { params },
      );
      return resp.data;
    } catch (err: any) {
      // 滚动部署兼容：旧后端没有 /score-series 时回退 /history（字段是超集，
      // 老响应缺 model/window，前端会按「无窗口」降级，不崩）。
      if (err?.response?.status === 404) {
        return this.client
          .get<StockScoreHistoryResponse>(
            `/models/inference/stock/${encodeURIComponent(opts.symbol)}/history`,
            { params },
          )
          .then((r) => r.data);
      }
      throw err;
    }
  }

  async deleteInferenceRun(runId: string): Promise<boolean> {
    try {
      await this.client.delete(`/models/inference/runs/${runId}`);
      return true;
    } catch {
      return false;
    }
  }

  async deleteInferenceHistory(runId: string): Promise<{ deleted: boolean; run_id: string }> {
    const resp = await this.client.delete<{ deleted: boolean; run_id: string }>(`/models/inference/runs/${runId}`);
    return resp.data;
  }


  async getAutoInferenceSettings(modelId: string): Promise<AutoInferenceSettings> {
    const resp = await this.client.get<AutoInferenceSettings>(`/models/inference/settings/${modelId}`);
    const data = resp.data as any;
    return {
      enabled: Boolean(data?.enabled),
      schedule_desc: String(data?.schedule_desc ?? ''),
      schedule_time: data?.schedule_time ? String(data.schedule_time) : undefined,
      last_run: data?.last_run_json ? this.normalizeInferenceRun(data.last_run_json) : data?.last_run ? this.normalizeInferenceRun(data.last_run) : undefined,
      next_run: data?.next_run ? String(data.next_run) : data?.next_run_at ? String(data.next_run_at) : undefined,
      updated_at: data?.updated_at ? String(data.updated_at) : undefined,
    };
  }

  async getLatestInferenceRun(
    modelId?: string,
    opts?: { perModel?: boolean },
  ): Promise<LatestInferenceRunInfo | null> {
    const resp = await this.client.get<LatestInferenceRunInfo>('/models/inference/latest', {
      params: {
        ...(modelId ? { model_id: modelId } : {}),
        ...(opts?.perModel ? { per_model: true } : {}),
      },
    });
    const data = resp.data as any;
    return {
      latest_key: String(data?.latest_key ?? ''),
      run_id: String(data?.run_id ?? ''),
      model_id: String(data?.model_id ?? ''),
      prediction_trade_date: data?.prediction_trade_date ? String(data.prediction_trade_date) : undefined,
      target_date: data?.target_date ? String(data.target_date) : undefined,
      status: data?.status ? String(data.status) : undefined,
      updated_at: data?.updated_at ? String(data.updated_at) : undefined,
      matched_model: typeof data?.matched_model === 'boolean' ? data.matched_model : null,
    };
  }

  async saveAutoInferenceSettings(modelId: string, settings: AutoInferenceSettings): Promise<AutoInferenceSettings> {
    const resp = await this.client.put<AutoInferenceSettings>(`/models/inference/settings/${modelId}`, {
      enabled: settings.enabled,
      schedule_time: settings.schedule_time,
    });
    const data = resp.data as any;
    return {
      enabled: Boolean(data?.enabled),
      schedule_desc: String(data?.schedule_desc ?? ''),
      schedule_time: data?.schedule_time ? String(data.schedule_time) : undefined,
      last_run: data?.last_run_json ? this.normalizeInferenceRun(data.last_run_json) : data?.last_run ? this.normalizeInferenceRun(data.last_run) : undefined,
      next_run: data?.next_run ? String(data.next_run) : data?.next_run_at ? String(data.next_run_at) : undefined,
      updated_at: data?.updated_at ? String(data.updated_at) : undefined,
    };
  }

  // ── 滚动回测（模型预测质量评估） ──

  async runModelBacktest(params: {
    model_id: string;
    start_date: string;
    end_date: string;
    horizon?: number;
    sample_interval?: number;
    cost?: TradingCostParams;
    exclude_limit_moves?: boolean;
  }): Promise<any> {
    const resp = await this.client.post('/admin/models/backtest', params, { timeout: 300000 });
    return resp.data;
  }

  async listModelsForBacktest(): Promise<any[]> {
    const resp = await this.client.get('/admin/models/list-for-backtest');
    return (resp.data as any)?.models ?? [];
  }

  async getBacktestTradingDates(modelId: string, start: string, end: string): Promise<string[]> {
    const resp = await this.client.get('/admin/models/backtest/trading-dates', {
      params: { model_id: modelId, start, end },
    });
    return (resp.data as any)?.dates ?? [];
  }

  async getBacktestHistory(modelId: string, limit = 20): Promise<any[]> {
    const resp = await this.client.get(`/admin/models/backtest/history/${modelId}`, {
      params: { limit },
    });
    return (resp.data as any)?.records ?? [];
  }

  async getBacktestDetail(modelId: string, runId: string): Promise<any> {
    const resp = await this.client.get(`/admin/models/backtest/history/${modelId}/${runId}`);
    return resp.data;
  }

  async deleteBacktestHistory(modelId: string, runId: string): Promise<void> {
    await this.client.delete(`/admin/models/backtest/history/${modelId}/${runId}`);
  }

  // ── 批量多日推理 ──

  async submitBatchInference(params: BatchInferenceRequest): Promise<BatchInferenceRecord> {
    const resp = await this.client.post<BatchInferenceRecord>('/models/inference/batch', params);
    return resp.data;
  }

  async getBatchInference(batchId: string): Promise<BatchInferenceRecord> {
    const resp = await this.client.get<BatchInferenceRecord>(`/models/inference/batch/${batchId}`);
    return resp.data;
  }

  async listBatchInferences(modelId?: string, status?: string, page = 1, pageSize = 20): Promise<{
    items: BatchInferenceRecord[];
    total: number;
  }> {
    const resp = await this.client.get('/models/inference/batches', {
      params: {
        model_id: modelId,
        status,
        page,
        page_size: pageSize,
      },
    });
    return resp.data;
  }

  async deleteBatchInference(batchId: string): Promise<{ deleted: boolean }> {
    const resp = await this.client.delete(`/models/inference/batch/${batchId}`);
    return resp.data;
  }

  async getBatchAggregate(
    batchId: string,
    params?: {
      top_k?: number;
      decay?: number;
      lam?: number;
      mu?: number;
      min_coverage?: number;
      consensus_band?: number;
      side?: 'long' | 'short' | 'both';
    },
  ): Promise<BatchAggregateResult> {
    const resp = await this.client.get<BatchAggregateResult>(
      `/models/inference/batch/${batchId}/aggregate`,
      { params },
    );
    return resp.data;
  }
}

export const modelTrainingService = new ModelTrainingService();
