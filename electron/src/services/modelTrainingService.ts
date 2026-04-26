import axios, { AxiosInstance } from 'axios';
import { SERVICE_ENDPOINTS } from '../config/services';
import { authService } from '../features/auth/services/authService';
import { AdminModelFeatureCatalog } from '../features/admin/types';

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
  metadata_json: Record<string, unknown>;
  metrics_json: Record<string, unknown>;
  is_default: boolean;
  created_at?: string | null;
  updated_at?: string | null;
  activated_at?: string | null;
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
}

export interface InferenceRankingResult {
  run_id: string;
  inference_date: string;
  target_date: string;
  model_id: string;
  summary?: InferenceRunRecord;
  rankings: InferenceRankingItem[];
}

export interface AutoInferenceSettings {
  enabled: boolean;
  schedule_desc: string;
  schedule_time?: string;
  last_run?: InferenceRunRecord;
  next_run?: string;
  updated_at?: string;
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

// ─── 系统内置模型（来自 models/production 目录）─────────────────────────────

export interface SystemModelRecord {
  model_id: string;
  dir_name: string;
  tenant_id: 'system';
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
  private readonly baseURL = (import.meta as any).env?.VITE_USER_API_URL || SERVICE_ENDPOINTS.USER_SERVICE;

  constructor() {
    this.client = axios.create({
      baseURL: this.baseURL,
      timeout: 30000,
      headers: {
        'Content-Type': 'application/json',
      },
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

  async getFeatureCatalog(): Promise<AdminModelFeatureCatalog> {
    const resp = await this.client.get<AdminModelFeatureCatalog>('/models/feature-catalog');
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

  async listUserModels(includeArchived = false): Promise<{ items: UserModelRecord[]; total: number }> {
    const resp = await this.client.get<{ items: UserModelRecord[]; total: number }>(`/models`, {
      params: { include_archived: includeArchived },
    });
    return resp.data;
  }

  async listSystemModels(): Promise<SystemModelRecord[]> {
    try {
      const resp = await this.client.get<{ status: string; count: number; models: SystemModelRecord[] }>(
        `/models/system-models`
      );
      return resp.data.models ?? [];
    } catch {
      return [];
    }
  }

  async getDefaultModel(): Promise<UserModelRecord> {
    const resp = await this.client.get<UserModelRecord>(`/models/default`);
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
    };
  }

  async runModelInference(modelId: string, inferenceDate: string): Promise<InferenceExecutionResult> {
    const resp = await this.client.post<InferenceExecutionResult>('/models/inference/run', {
      model_id: modelId,
      inference_date: inferenceDate,
    });
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
        name: item.symbol,
        score: Number(item.fusion_score ?? 0),
        signal: item.signal_side === 'buy' ? 'buy' : item.signal_side === 'sell' ? 'sell' : 'hold',
      })),
    };
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
}

export const modelTrainingService = new ModelTrainingService();
