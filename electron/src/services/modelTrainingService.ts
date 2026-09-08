import axios, { AxiosInstance } from 'axios';
import { SERVICE_ENDPOINTS, resolveWebSafeServiceBase } from '../config/services';
import { authService } from '../features/auth/services/authService';
import {
  AdminModelFeatureCatalog,
  QuantDBTrainingSourcesResult,
} from '../features/admin/types';

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
}

export interface CreateEnsembleParams {
  source_model_ids: string[];
  display_name?: string;
  weight_strategy?: 'equal' | 'icir' | 'manual' | 'recent_ic';
  manual_weights?: Record<string, number>;
  fusion_strategy?: 'linear' | 'majority_vote' | 'periodic_hierarchy' | 'confidence_gate';
  strategy_config?: Record<string, number>;
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
}

export interface StockScoreHistoryResponse {
  symbol: string;
  normalized_symbol: string;
  name: string;
  industry: string;
  board?: string;
  total: number;
  items: StockScoreHistoryItem[];
  models?: Array<{ model_id: string; display_name?: string }>;
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

  async runTraining<T extends object>(payload: T): Promise<ModelTrainingRunResponse> {
    const resp = await this.client.post<ModelTrainingRunResponse>('/models/run-training', payload);
    return resp.data;
  }

  async getTrainingRun(runId: string): Promise<ModelTrainingRunStatus> {
    const resp = await this.client.get<ModelTrainingRunStatus>(`/models/training-runs/${runId}`);
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

  async setDefaultModel(modelId: string): Promise<UserModelRecord> {
    const resp = await this.client.patch<UserModelRecord>(`/models/default`, { model_id: modelId });
    return resp.data;
  }

  async getUserModel(modelId: string): Promise<UserModelRecord> {
    const resp = await this.client.get<UserModelRecord>(`/models/${modelId}`);
    return resp.data;
  }

  async createEnsemble(params: CreateEnsembleParams): Promise<UserModelRecord> {
    const resp = await this.client.post<UserModelRecord>('/models/ensemble/create', params);
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

  async activateUserModel(modelId: string): Promise<UserModelRecord> {
    const resp = await this.client.post<UserModelRecord>(`/models/${modelId}/activate`);
    return resp.data;
  }

  async getModelDrift(modelId: string): Promise<any> {
    const resp = await this.client.get<any>(`/models/${encodeURIComponent(modelId)}/drift`);
    return resp.data;
  }

  async getMarketRegime(modelId: string, window = 90): Promise<any> {
    const resp = await this.client.get<any>(`/models/${encodeURIComponent(modelId)}/market-regime`, { params: { window } });
    return resp.data;
  }

  async getInferenceCoverage(modelId: string): Promise<any> {
    const resp = await this.client.get<any>(`/models/${encodeURIComponent(modelId)}/inference/coverage`);
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

  async compareModels(modelIds: string[]): Promise<any> {
    const resp = await this.client.post<any>('/models/compare', { model_ids: modelIds });
    return resp.data;
  }

  async getStrategyBinding(strategyId: string): Promise<StrategyModelBinding> {
    const resp = await this.client.get<StrategyModelBinding>(`/models/strategy-bindings/${strategyId}`);
    return resp.data;
  }

  async setStrategyBinding(strategyId: string, modelId: string): Promise<StrategyModelBinding> {
    const resp = await this.client.put<StrategyModelBinding>(`/models/strategy-bindings/${strategyId}`, {
      model_id: modelId,
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
    };
  }

  async runModelInference(modelId: string, inferenceDate: string): Promise<InferenceExecutionResult> {
    const resp = await this.client.post<InferenceExecutionResult>('/models/inference/run', {
      model_id: modelId,
      inference_date: inferenceDate,
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

  async getInferenceResult(runId: string): Promise<InferenceRankingResult> {
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
    }>(`/models/inference/runs/${runId}`);

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
      })),
    };
  }

  async getStockInferenceHistory(symbol: string, days = 180, modelId?: string, endDate?: string): Promise<StockScoreHistoryResponse> {
    const resp = await this.client.get<StockScoreHistoryResponse>(
      `/models/inference/stock/${encodeURIComponent(symbol)}/history`,
      { params: { days, ...(modelId ? { model_id: modelId } : {}), ...(endDate ? { end_date: endDate } : {}) } },
    );
    return resp.data;
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

  async getLatestInferenceRun(modelId?: string): Promise<LatestInferenceRunInfo | null> {
    const resp = await this.client.get<LatestInferenceRunInfo>('/models/inference/latest', {
      params: modelId ? { model_id: modelId } : undefined,
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

  async runMultiHorizonBacktest(params: {
    model_id: string;
    start_date: string;
    end_date: string;
    horizons?: number[];
    sample_interval?: number;
    cost?: TradingCostParams;
    exclude_limit_moves?: boolean;
  }): Promise<any> {
    const resp = await this.client.post('/admin/models/backtest/multi-horizon', params, { timeout: 600000 });
    return resp.data;
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
