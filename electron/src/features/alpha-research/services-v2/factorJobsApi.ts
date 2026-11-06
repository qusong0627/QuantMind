import { apiClient } from '../../../services/aiStrategyClients';
import { generateId } from '../utils-v2';

export type FactorJobStatus =
  | 'queued'
  | 'running'
  | 'stopping'
  | 'completed'
  | 'failed'
  | 'cancelled';

export interface FactorCurvePoint {
  date: string;
  value: number;
}

export interface FactorBacktestSeries {
  dates?: string[];
  nav_long?: Array<number | null>;
  nav_long_gross?: Array<number | null>;
  nav_bench?: Array<number | null>;
  nav_excess_pct?: Array<number | null>;
  nav_long_short_gross?: Array<number | null>;
  drawdown?: Array<number | null>;
  ic?: Array<number | null>;
  rank_ic?: Array<number | null>;
  turnover?: Array<number | null>;
  coverage?: number[];
  q_curves?: Record<string, Array<number | null>>;
  turnover_basis?: string;
  equity_curve?: FactorCurvePoint[];
  drawdown_curve?: FactorCurvePoint[];
  basis?: string;
  cost_rate?: number;
}

export interface FactorCostModelSnapshot {
  commission_rate: number;
  min_commission: number;
  stamp_duty: number;
  transfer_fee: number;
  slippage: number;
  round_trip_cost: number;
  round_trip_cost_sh: number;
}

export interface MaterializationEvidence {
  automatic_check?: 'skipped' | 'completed';
  deduplication?: 'manual' | 'automatic';
  corr_unverified?: boolean;
  sample_days?: number;
  min_sample_days?: number;
  min_pairs_per_day?: number;
  control_columns?: number;
  factory_skipped?: boolean | number;
  corr_threshold?: number;
  ic_floor?: number;
  ic_ratio?: number;
  opponent?: { kind?: string; factor_id?: string } | null;
  replacement?: {
    decision?: string;
    hits?: string[];
    new_ic?: number | null;
    incumbent_ic?: number | null;
  };
}

export interface FactorJob {
  job_id: string;
  kind: 'backtest' | 'validate' | 'materialize' | 'compose' | 'doc_parse' | 'doc_organize' | 'cleanup' | 'recovery' | 'pool_research' | 'purge' | 'purge_task';
  status: FactorJobStatus;
  execution_generation: number;
  request: {
    task_ids?: string[];
    task_id?: string;
    action?: string;
    doc_id?: string;
    organize_kind?: 'free' | 'paper';
    factor_id?: string;
    factor_ids?: string[];
    market?: 'a_share';
    name?: string;
    seed?: number;
    batch_id?: string;
    batch_size?: number;
    universe?: string;
    start_date?: string;
    end_date?: string;
    data_source?: 'qlib_bin' | 'h5';
    cost_model?: FactorCostModelSnapshot;
    validation_context?: {
      kind: 'out_of_sample_validation';
      training_window: { start_date: string; end_date: string };
      validation_window: { start_date: string; end_date: string };
      non_overlapping: boolean;
    };
  };
  progress_snapshot?: Record<string, unknown>;
  result_snapshot?: {
    status?: string;
    generation_id?: string;
    replaced_generation_id?: string | null;
    replaced_factor_id?: string | null;
    new_ic?: number | null;
    incumbent_ic?: number | null;
    corr?: number | null;
    corr_against?: string | null;
    evidence?: MaterializationEvidence;
    library_gate?: {
      corr?: number | null;
      corr_against?: string | null;
      evidence?: MaterializationEvidence;
    };
    metrics?: Record<string, number | null>;
    series?: FactorBacktestSeries;
    validation?: {
      training_window?: { start_date?: string; end_date?: string };
      validation_window?: { start_date?: string; end_date?: string };
      non_overlapping?: boolean;
    };
    factor_ids?: string[];
    factor_count?: number;
    results?: Array<{
      status?: string;
      error?: string;
      factor_revision_no?: number | null;
      factor_id?: string;
      metrics?: Record<string, number | null>;
      series?: FactorBacktestSeries;
      validation?: {
        training_window?: { start_date?: string; end_date?: string };
        validation_window?: { start_date?: string; end_date?: string };
        non_overlapping?: boolean;
      };
    }>;
    [key: string]: unknown;
  };
  error_code?: string | null;
  error_message?: string | null;
  created_at?: string;
  updated_at?: string;
  completed_at?: string | null;
}

export interface FactorLifecycleReading {
  month_end: string;
  n_days: number;
  icir: number | null;
  status: string;
  below_floor: boolean | null;
}

export interface FactorLifecycleEvaluation {
  factor_id: string;
  state: 'experiment' | 'observing' | 'active' | 'downweighted';
  blind_until: string | null;
  eval_status: string;
  decision?: string;
  streak?: number;
  latest?: FactorLifecycleReading;
  readings?: FactorLifecycleReading[];
  action: 'none' | 'already' | 'downweight';
  applied: boolean;
  downweighted?: boolean;
}

export interface FactorLifecycleEvent {
  event_id: number;
  event_type: 'evaluation' | 'manual_restore';
  evaluated_at: string;
  factor_revision_no?: number | null;
  evidence_job_id?: string | null;
  event_snapshot?: Record<string, unknown>;
  redacted?: boolean;
  eval_status?: string;
}

export interface FactorJobHistoryPage {
  jobs: FactorJob[];
  has_more: boolean;
  limit: number;
  offset: number;
}

export interface FactorValidationAttempt {
  validation_id: string;
  factor_id: string;
  status: 'completed' | 'failed';
  validation_context?: {
    training_window?: { start_date?: string; end_date?: string };
    validation_window?: { start_date?: string; end_date?: string };
    non_overlapping?: boolean;
  };
  result_snapshot?: { metrics?: Record<string, number | null> };
  error_code?: string | null;
  created_at?: string;
  is_latest?: boolean;
}

export interface FactorSourceRevision {
  revision_no: number;
  factor_name: string;
  factor_code: string;
  factor_code_sha256: string;
  factor_formulation: string | null;
  created_at: string;
}

export async function getFactorSourceRevisions(
  factorId: string,
  signal?: AbortSignal,
): Promise<FactorSourceRevision[]> {
  const response = await apiClient.get(
    `/alpha-agent/factors/${encodeURIComponent(factorId)}/revisions`,
    { params: { limit: 20 }, signal },
  );
  const rows = response.data?.data?.revisions;
  if (!Array.isArray(rows)) throw new Error('因子代码版本格式无效');
  return rows as FactorSourceRevision[];
}

export async function setFactorArchived(
  factorId: string,
  archived: boolean,
  signal?: AbortSignal,
): Promise<{ factor_id: string; archived_at: string | null }> {
  const response = await apiClient.post(
    `/alpha-agent/factors/${encodeURIComponent(factorId)}/${archived ? 'archive' : 'restore'}`,
    { reason: archived ? '因子库手动归档' : '因子库手动恢复' },
    { signal },
  );
  const result = response.data?.data;
  if (!result || result.factor_id !== factorId || (archived ? typeof result.archived_at !== 'string' : result.archived_at !== null)) {
    throw new Error('因子归档状态回执无效，请刷新列表核对');
  }
  return result;
}

export interface FactorPoolEntry {
  factor_id: string;
  factor_name: string;
  market: 'a_share';
  universe: string;
  pool_score: number | null;
  novelty: number | null;
  max_pool_corr: number | null;
  max_pool_corr_with: string | null;
  diversity_contrib: number | null;
  times_retrieved: number;
  panel_version: string | null;
  status: string;
  ic_value: number | null;
  rank_ic: number | null;
  sharpe_ratio: number | null;
}

export interface FactorPoolEdge {
  universe: string;
  src_factor_id: string;
  dst_factor_id: string;
  src_factor_name: string;
  dst_factor_name: string;
  relation: 'same_task' | 'similar_to' | 'correlated_with';
  method: 'formula' | 'value' | 'task_round';
  weight: number | null;
  signed_correlation: number | null;
}

export interface FactorPoolEdgeRebuild {
  factor_count: number;
  edge_count: number;
  threshold: number;
  top_k: number;
}

export interface FactorPoolValueEdgeRebuild {
  panel_count: number;
  edge_count: number;
  threshold: number;
  top_k: number;
}

export interface FactorPoolScoreRebuild {
  member_count: number;
  scored_count: number;
  missing_icir_count: number;
  updated_count: number;
  universe_count: number;
}

export interface FactorPoolPanelPoint {
  trade_date: string;
  coverage: number;
  mean_rank_pct: number;
  mean_zscore: number;
}

export interface FactorPoolPanelSummary {
  factor_id: string;
  market: 'a_share';
  universe: string;
  panel_version: string;
  points: FactorPoolPanelPoint[];
}

export async function listFactorPool(
  params: { universe?: string; include_archived?: boolean; limit?: number } = {},
  signal?: AbortSignal,
): Promise<FactorPoolEntry[]> {
  const response = await apiClient.get('/alpha-agent/factors/pool', { params, signal });
  const data = response.data?.data;
  if (!data || !Array.isArray(data.items)) throw new Error('因子池格式无效');
  return data.items as FactorPoolEntry[];
}

export async function getFactorPoolPanel(
  factorId: string,
  params: { market?: 'a_share'; universe?: string } = {},
  signal?: AbortSignal,
): Promise<FactorPoolPanelSummary> {
  const response = await apiClient.get(
    `/alpha-agent/factors/pool/${encodeURIComponent(factorId)}/panel`,
    { params: { market: params.market ?? 'a_share', universe: params.universe ?? '' }, signal },
  );
  const data = response.data?.data;
  if (!data || data.factor_id !== factorId || !Array.isArray(data.points)) {
    throw new Error('本人因子面板格式无效');
  }
  return data as FactorPoolPanelSummary;
}

export async function addFactorToPool(
  factorId: string,
  params: { market?: 'a_share'; universe?: string } = {},
  signal?: AbortSignal,
): Promise<void> {
  await apiClient.post(
    `/alpha-agent/factors/pool/${encodeURIComponent(factorId)}`,
    { market: params.market ?? 'a_share', universe: params.universe ?? '' },
    { signal },
  );
}

export async function removeFactorFromPool(
  factorId: string,
  params: { market?: 'a_share'; universe?: string } = {},
  signal?: AbortSignal,
): Promise<void> {
  await apiClient.delete(
    `/alpha-agent/factors/pool/${encodeURIComponent(factorId)}`,
    { params: { market: params.market ?? 'a_share', universe: params.universe ?? '' }, signal },
  );
}

export async function rebuildFactorPoolEdges(
  params: { market?: 'a_share'; universe?: string } = {},
  signal?: AbortSignal,
): Promise<FactorPoolEdgeRebuild> {
  const response = await apiClient.post(
    '/alpha-agent/factors/pool/graph/rebuild',
    { market: params.market ?? 'a_share', universe: params.universe },
    { signal },
  );
  const data = response.data?.data;
  if (!data || typeof data.edge_count !== 'number') throw new Error('谱系重算结果格式无效');
  return data as FactorPoolEdgeRebuild;
}

export async function rebuildFactorPoolValueEdges(
  params: { market?: 'a_share'; universe?: string } = {},
  signal?: AbortSignal,
): Promise<FactorPoolValueEdgeRebuild> {
  const response = await apiClient.post(
    '/alpha-agent/factors/pool/graph/value-rebuild',
    { market: params.market ?? 'a_share', universe: params.universe },
    { signal },
  );
  const data = response.data?.data;
  if (!data || typeof data.edge_count !== 'number' || typeof data.panel_count !== 'number') {
    throw new Error('值相关性重算结果格式无效');
  }
  return data as FactorPoolValueEdgeRebuild;
}

export async function rebuildFactorPoolTaskRoundEdges(
  params: { market?: 'a_share'; universe?: string } = {},
  signal?: AbortSignal,
): Promise<{ factor_count: number; task_count: number; edge_count: number }> {
  const response = await apiClient.post(
    '/alpha-agent/factors/pool/graph/task-round-rebuild',
    { market: params.market ?? 'a_share', universe: params.universe },
    { signal },
  );
  const data = response.data?.data;
  if (
    !data
    || typeof data.factor_count !== 'number'
    || typeof data.task_count !== 'number'
    || typeof data.edge_count !== 'number'
  ) throw new Error('任务轮次谱系重算结果格式无效');
  return data as { factor_count: number; task_count: number; edge_count: number };
}

export async function rebuildFactorPoolScores(
  params: { market?: 'a_share'; universe?: string } = {},
  signal?: AbortSignal,
): Promise<FactorPoolScoreRebuild> {
  const response = await apiClient.post(
    '/alpha-agent/factors/pool/score/rebuild',
    { market: params.market ?? 'a_share', universe: params.universe },
    { signal },
  );
  const data = response.data?.data;
  if (!data || typeof data.updated_count !== 'number' || typeof data.scored_count !== 'number') {
    throw new Error('因子池评分重算结果格式无效');
  }
  return data as FactorPoolScoreRebuild;
}

export async function listFactorPoolEdges(
  params: { universe?: string; limit?: number } = {},
  signal?: AbortSignal,
): Promise<FactorPoolEdge[]> {
  const response = await apiClient.get('/alpha-agent/factors/pool/graph/edges', { params, signal });
  const items = response.data?.data?.items;
  if (!Array.isArray(items)) throw new Error('因子谱系格式无效');
  return items as FactorPoolEdge[];
}

function unwrapJob(value: unknown): FactorJob {
  const wrapped = typeof value === 'object' && value !== null && 'data' in value
    ? (value as { data?: unknown }).data ?? value
    : value;
  if (
    typeof wrapped !== 'object'
    || wrapped === null
    || !('job_id' in wrapped)
    || typeof (wrapped as { job_id?: unknown }).job_id !== 'string'
  ) {
    throw new Error('后端没有返回有效的回测任务');
  }
  return wrapped as FactorJob;
}

export async function listFactorJobs(signal?: AbortSignal): Promise<FactorJob[]> {
  const response = await apiClient.get('/alpha-agent/factor-jobs', {
    params: { limit: 100, offset: 0 },
    signal,
  });
  const rows = response.data?.data;
  if (!Array.isArray(rows)) throw new Error('回测任务列表格式无效');
  return (rows as unknown[]).map(unwrapJob);
}

export async function getFactorJobHistory(
  params: { limit: number; offset: number; status?: string },
  signal?: AbortSignal,
): Promise<FactorJobHistoryPage> {
  const response = await apiClient.get('/alpha-agent/factor-jobs/history', { params, signal });
  const data = response.data?.data;
  if (!data || !Array.isArray(data.tasks)) throw new Error('因子作业历史格式无效');
  return {
    jobs: (data.tasks as unknown[]).map(unwrapJob),
    has_more: data.has_more === true,
    limit: Number(data.limit ?? params.limit),
    offset: Number(data.offset ?? params.offset),
  };
}

export async function getFactorValidationHistory(
  factorId: string,
  signal?: AbortSignal,
): Promise<{ latest_validation_id: string | null; attempts: FactorValidationAttempt[] }> {
  const response = await apiClient.get(
    `/alpha-agent/factors/${encodeURIComponent(factorId)}/validation-history`,
    { params: { limit: 20 }, signal },
  );
  const data = response.data?.data;
  if (!data || !Array.isArray(data.attempts)) throw new Error('验证记录格式无效');
  return {
    latest_validation_id: typeof data.latest_validation_id === 'string'
      ? data.latest_validation_id
      : null,
    attempts: data.attempts as FactorValidationAttempt[],
  };
}

export async function submitFactorJob(
  request: {
    factor_id: string;
    kind?: 'backtest' | 'validate';
    universe?: string;
    start_date?: string;
    end_date?: string;
    data_source: 'qlib_bin';
  },
): Promise<FactorJob> {
  const idempotencyKey = globalThis.crypto?.randomUUID?.() ?? generateId();
  const response = await apiClient.post('/alpha-agent/factor-jobs', request, {
    headers: { 'Idempotency-Key': idempotencyKey },
  });
  return unwrapJob(response.data);
}

export async function submitFactorBatch(
  request: {
    factor_ids: string[];
    kind?: 'backtest' | 'validate';
    universe: string;
    start_date?: string;
    end_date?: string;
    data_source: 'qlib_bin';
  },
): Promise<FactorJob> {
  const idempotencyKey = globalThis.crypto?.randomUUID?.() ?? generateId();
  const response = await apiClient.post('/alpha-agent/factor-jobs/batch', request, {
    headers: { 'Idempotency-Key': idempotencyKey },
  });
  return unwrapJob(response.data);
}

export async function submitFactorMaterialization(request: {
  factor_id: string;
  universe?: string;
  start_date?: string;
  end_date?: string;
}): Promise<FactorJob> {
  const idempotencyKey = globalThis.crypto?.randomUUID?.() ?? generateId();
  const response = await apiClient.post('/alpha-agent/factor-jobs/materialize', request, {
    headers: { 'Idempotency-Key': idempotencyKey },
  });
  return unwrapJob(response.data);
}

export async function submitFactorCombo(request: {
  factor_ids: string[];
  universe: string;
  name?: string;
  seed?: number;
}): Promise<FactorJob> {
  const idempotencyKey = globalThis.crypto?.randomUUID?.() ?? generateId();
  const response = await apiClient.post('/alpha-agent/factor-jobs/compose', request, {
    headers: { 'Idempotency-Key': idempotencyKey },
  });
  return unwrapJob(response.data);
}

export async function evaluateFactorLifecycle(
  factorId: string,
  apply = false,
  signal?: AbortSignal,
): Promise<FactorLifecycleEvaluation> {
  const query = apply ? '?apply=true' : '';
  const response = await apiClient.post(
    `/alpha-agent/factors/${encodeURIComponent(factorId)}/lifecycle/evaluate${query}`,
    undefined,
    { signal },
  );
  const result = response.data?.data;
  if (!result || typeof result.factor_id !== 'string') {
    throw new Error('因子生命周期评估响应无效');
  }
  return result as FactorLifecycleEvaluation;
}

export async function restoreFactorLifecycle(
  factorId: string,
  reason: string,
  signal?: AbortSignal,
): Promise<{ factor_id: string; lifecycle: Record<string, unknown> }> {
  const response = await apiClient.post(
    `/alpha-agent/factors/${encodeURIComponent(factorId)}/lifecycle/restore`,
    { reason },
    { signal },
  );
  const result = response.data?.data;
  if (!result || typeof result.factor_id !== 'string') {
    throw new Error('因子生命周期恢复响应无效');
  }
  return result;
}

export async function getFactorLifecycleHistory(
  factorId: string,
  options: { limit?: number; beforeEventId?: number } = {},
  signal?: AbortSignal,
): Promise<{ events: FactorLifecycleEvent[]; next_before_event_id: number | null }> {
  const params = new URLSearchParams();
  if (options.limit !== undefined) params.set('limit', String(options.limit));
  if (options.beforeEventId !== undefined) params.set('before_event_id', String(options.beforeEventId));
  const query = params.size ? `?${params.toString()}` : '';
  const response = await apiClient.get(
    `/alpha-agent/factors/${encodeURIComponent(factorId)}/lifecycle/history${query}`,
    { signal },
  );
  const result = response.data?.data;
  if (!result || !Array.isArray(result.events)) {
    throw new Error('因子生命周期历史响应无效');
  }
  return result as { events: FactorLifecycleEvent[]; next_before_event_id: number | null };
}

export async function cancelFactorJob(jobId: string, signal?: AbortSignal): Promise<FactorJob> {
  const response = await apiClient.post(
    `/alpha-agent/factor-jobs/${encodeURIComponent(jobId)}/cancel`,
    undefined,
    { signal },
  );
  return unwrapJob(response.data);
}

/**
 * 清除一条已结束的回测/验证记录（服务端硬删，列表与后端数据一起消失）。
 * 运行中的任务服务端返回 409，必须先取消。
 */
export async function deleteFactorJob(jobId: string, signal?: AbortSignal): Promise<void> {
  await apiClient.delete(`/alpha-agent/factor-jobs/${encodeURIComponent(jobId)}`, { signal });
}

export async function downloadFactorJobReport(
  jobId: string,
  signal?: AbortSignal,
): Promise<Blob> {
  const response = await apiClient.get(
    `/alpha-agent/factor-jobs/${encodeURIComponent(jobId)}/report.pdf`,
    { responseType: 'blob', signal },
  );
  if (!(response.data instanceof Blob)) throw new Error('PDF 报告格式无效');
  return response.data;
}

export interface FactorExportColumn {
  factor_id: string;
  generation_id: string;
  feature_key: string;
  column?: string;
}

export interface FactorExportSummary {
  export_id: string;
  name: string;
  columns: FactorExportColumn[];
  row_count: number;
  column_count: number;
  byte_count: number;
  date_min: string;
  date_max: string;
  symbol_count: number;
  download_path: string;
}

export async function submitFactorExport(request: {
  factor_ids: string[];
  start_date?: string;
  end_date?: string;
  name?: string;
}): Promise<FactorExportSummary> {
  const response = await apiClient.post('/alpha-agent/factors/export', request);
  const data = response.data?.data;
  if (!data || typeof data.export_id !== 'string') throw new Error('因子合并导出响应无效');
  return data as FactorExportSummary;
}

export async function downloadFactorExport(
  exportId: string,
  signal?: AbortSignal,
): Promise<Blob> {
  const response = await apiClient.get(
    `/alpha-agent/factors/exports/${encodeURIComponent(exportId)}/download`,
    { responseType: 'blob', signal },
  );
  if (!(response.data instanceof Blob)) throw new Error('导出文件格式无效');
  return response.data;
}
