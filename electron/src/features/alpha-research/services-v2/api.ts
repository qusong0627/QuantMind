/**
 * AutoAlpha frontend-v2 API bridge
 *
 * The original frontend-v2 expected its own dedicated FastAPI backend with
 * endpoints like /api/v1/mining/start, /api/v1/factors, WS /ws/mining/{id}.
 * In QuantMind we expose AlphaAgent under /api/v1/alpha-agent/* via the engine
 * service. This module preserves the original public surface (signatures,
 * return shapes) so the ported pages/components compile and run, but delegates
 * to the QuantMind alpha-agent router and normalizes the response shape.
 */

import { apiClient } from '../../../services/aiStrategyClients';
import { authService } from '../../auth/services/authService';
import { SERVICE_ENDPOINTS } from '../../../config/services';
import { generateId } from '../utils-v2';
import { requestResearchResult } from './researchRequestsApi';
import type {
  ApiResponse,
  DataSummary,
  Factor,
  FactorCategory,
  Task,
  TaskStatus,
  ExecutionPhase,
  RealtimeMetrics,
  UniverseId,
  UniverseInfo,
  WsMessage,
  StatisticalMiningOptions,
} from '../types-v2';

// ========================== Defaults ==========================

/** Display names for stock universes — kept in sync with backend UNIVERSE_NAMES */
export const UNIVERSE_LABELS: Record<UniverseId, string> = {
  csi300: '沪深300',
  csi500: '中证500',
  csi1000: '中证1000',
  sse50: '上证50',
  gem: '创业板指',
  star: '科创50',
  csi800: '中证800',
  all_a: '全部A股',
};

function makeOk<T>(data: T): ApiResponse<T> {
  return { success: true, data };
}

function emptyMetrics(): RealtimeMetrics {
  return {
    ic: null,
    icir: null,
    rankIc: null,
    rankIcir: null,
    annualReturn: null,
    sharpeRatio: null,
    maxDrawdown: null,
    totalFactors: 0,
    highQualityFactors: 0,
    mediumQualityFactors: 0,
    lowQualityFactors: 0,
    top10Factors: [],
  };
}

export function classifyQuality(ic: number | null | undefined): 'high' | 'medium' | 'low' {
  if (ic == null || !Number.isFinite(ic)) return 'low';
  const v = Math.abs(ic);
  if (v >= 0.05) return 'high';
  if (v >= 0.02) return 'medium';
  return 'low';
}

function normalizeTaskStatus(raw: string | undefined): TaskStatus {
  switch (raw) {
    case 'completed':
      return 'completed';
    case 'failed':
      return 'failed';
    case 'cancelled':
      return 'cancelled';
    case 'stopping':
      return 'stopping';
    case 'running':
      return 'running';
    case 'pending':
    case 'queued':
      return 'queued';
    default:
      return 'idle';
  }
}

const PHASE_MAP: Record<string, ExecutionPhase> = {
  pending: 'parsing',
  queued: 'parsing',
  starting: 'parsing',
  scenario: 'parsing',
  hypothesis: 'planning',
  experiment: 'planning',
  coder: 'evolving',
  runner: 'backtesting',
  summarizer: 'analyzing',
  completed: 'completed',
};

function normalizeAgentTask(raw: any, configHint?: any): Task {
  const status = normalizeTaskStatus(raw?.status);
  const backendPhase: string = typeof raw?.phase === 'string' ? raw.phase : '';
  let phase: ExecutionPhase = PHASE_MAP[backendPhase] || 'parsing';
  if (status === 'completed') phase = 'completed';
  else if (status === 'failed') phase = 'parsing';

  // Prefer backend's progress_pct; fall back to numeric progress; never fabricate 50%.
  let progressNum: number;
  if (typeof raw?.progress_pct === 'number') {
    progressNum = raw.progress_pct;
  } else if (typeof raw?.progress === 'number') {
    progressNum = raw.progress;
  } else if (status === 'completed') {
    progressNum = 100;
  } else if (status === 'failed') {
    progressNum = 0;
  } else if (status === 'running') {
    progressNum = 5;
  } else {
    progressNum = 0;
  }

  const currentRound = typeof raw?.current_loop === 'number' ? raw.current_loop : 0;
  const totalRounds = typeof raw?.loop_n === 'number' ? raw.loop_n : 0;

  return {
    taskId: raw?.task_id ?? raw?.taskId ?? '',
    queuePosition: typeof raw?.queue_position === 'number' ? raw.queue_position : undefined,
    status,
    config: configHint ?? { userInput: '' },
    progress: {
      phase,
      currentRound,
      totalRounds,
      progress: progressNum,
      message:
        status === 'queued'
          ? (raw?.queue_position ? `排队中，第 ${raw.queue_position} 位；可取消排队` : '排队中，等待前序任务完成，可取消排队')
          : typeof raw?.progress === 'string'
          ? raw.progress
          : raw?.error_message || (status === 'completed' ? '完成' : '运行中'),
      timestamp: raw?.updated_at ?? new Date().toISOString(),
    },
    // 回测任务由 getBacktestStatus 把持久化 factor-job 结果里的指标放进 raw.metrics；
    // 保留后端给的字段（缺失的键保持 undefined，前端据此显示 "--" 而不是伪造 0）。
    metrics: (raw?.metrics && Object.keys(raw.metrics).length > 0
      ? raw.metrics
      : emptyMetrics()) as RealtimeMetrics,
    logs: [],
    createdAt: raw?.created_at ?? new Date().toISOString(),
    updatedAt: raw?.updated_at ?? new Date().toISOString(),
    timeline: raw?.timeline ?? undefined,
    tokenUsage: raw?.token_usage ?? undefined,
    factors: Array.isArray(raw?.factors) ? raw.factors.map(normalizeAgentFactor) : [],
  };
}

function metricNumber(value: unknown): number | null {
  if (value == null || (typeof value !== 'number' && typeof value !== 'string') || String(value).trim() === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function normalizeAgentFactor(raw: any): Factor {
  const ic = metricNumber(raw?.ic_value);
  const meta = raw?.metadata ?? {};
  return {
    factorId: raw?.id ?? raw?.factor_id ?? '',
    factorName: raw?.factor_name ?? 'unnamed',
    factorExpression: raw?.factor_formulation ?? raw?.factor_code ?? '',
    factorDescription: meta.description ?? raw?.category ?? '',
    quality: classifyQuality(ic),
    market: meta.market ?? raw?.market ?? undefined,
    universe: raw?.universe ?? meta.universe ?? undefined,
    ic,
    icir: metricNumber(raw?.icir ?? meta.icir),
    rankIc: metricNumber(raw?.rank_ic ?? meta.rank_ic),
    rankIcir: metricNumber(raw?.rank_icir ?? meta.rank_icir),
    sharpeRatio: metricNumber(raw?.sharpe_ratio),
    annualReturn: metricNumber(raw?.annual_return),
    maxDrawdown: metricNumber(raw?.max_drawdown),
    round: meta.round ?? 0,
    direction: meta.direction ?? raw?.category ?? '',
    createdAt: raw?.created_at ?? '',
    archivedAt: raw?.archived_at ?? null,
    hasCode: typeof raw?.factor_code === 'string' && Boolean(raw.factor_code.trim()),
    archiveReason: raw?.archive_reason ?? null,
    tags: raw?.tags ?? undefined,
    lifecycle: raw?.lifecycle ?? undefined,
  };
}

// ========================== Mining API ==========================

export interface MiningStartParams {
  direction: string;
  algorithm?: 'rdagent' | 'statistical';
  statisticalConfig?: StatisticalMiningOptions;
  autoMaterialize?: boolean;
  miningStart?: string;
  miningEnd?: string;
  validStart?: string;
  validEnd?: string;
  market?: string;
  universe?: string;
  dataSource?: string;
  datasets?: string[];
  freq?: 'day';
  maxRounds?: number;
  /** L1 因子类别方向（多选，label） */
  directions?: string[];
  /** 类别选择模式：selected=取第一条，random=随机一条，quota=配额加权一条 */
  directionMode?: 'selected' | 'random' | 'quota';
}

export interface MiningPreview {
  algorithm: { key: string; name: string; version: string; uses_llm: boolean; uses_datasets: boolean };
  market: string;
  freq: string;
  universe: string;
  data_source: string;
  loop_n: number;
  direction: string;
  directions: string[];
  direction_mode: 'selected' | 'random' | 'quota';
  direction_weighting?: 'blankness' | null;
  direction_quota?: {
    available: boolean;
    n: number;
    quota: { explore: number; exploit: number; mutate: number };
    slots: { explore: number; exploit: number; mutate: number };
    saturated_threshold: number;
    recent_success_tasks: number;
    directions: Array<{
      direction: string; attempts: number; n_tasks: number; n_success: number;
      p: number; dup_hits_90d: number; sat_penalty: number; score: number; saturated: boolean;
    }>;
    tiers: {
      explore: { pool: string[]; weighting: string; fallback: string | null };
      exploit: { pool: string[]; weighting: string; fallback: string | null };
      mutate: { pool: string[]; weighting: string; fallback: string | null };
    } | null;
    recent_success_dirs: string[];
    error: string | null;
  } | null;
  datasets: Record<string, string>;
  window: {
    mode: 'custom' | 'server_default';
    mining?: [string, string];
    train?: [string, string];
    valid?: [string, string];
    test?: [string, string];
    oos?: [string, string] | null;
  };
  requires_llm: boolean;
  statistical_config?: StatisticalMiningOptions | null;
}

function miningRequestBody(params: MiningStartParams) {
  const loopN = params.maxRounds ?? 3;
  if (params.algorithm === 'statistical') {
    return {
      ...params.statisticalConfig,
      algorithm: 'statistical',
      market: 'a_share',
      universe: params.universe ?? 'all_a',
      data_source: 'qlib_bin',
      freq: params.freq ?? 'day',
      mining_start: params.miningStart,
      mining_end: params.miningEnd,
      valid_start: params.validStart,
      valid_end: params.validEnd,
      auto_materialize: params.autoMaterialize ?? true,
    };
  }
  return {
    algorithm: 'rdagent',
    market: params.market ?? 'a_share',
    universe: params.universe ?? 'all_a',
    loop_n: loopN,
    direction: params.direction || '',
    directions: params.directions?.filter((value) => value.trim()) ?? [],
    direction_mode: params.directionMode ?? 'selected',
    data_source: params.dataSource ?? 'qlib_bin',
    freq: params.freq ?? 'day',
    datasets: params.datasets,
    mining_start: params.miningStart,
    mining_end: params.miningEnd,
    valid_start: params.validStart,
    valid_end: params.validEnd,
  };
}

export async function previewMining(
  params: MiningStartParams,
): Promise<ApiResponse<MiningPreview>> {
  const res = await apiClient.post('/alpha-agent/preview', miningRequestBody(params));
  return makeOk(res.data?.data as MiningPreview);
}

export async function startMining(
  params: MiningStartParams,
  requestKey: string,
  signal?: AbortSignal,
): Promise<ApiResponse<{ taskId: string; task: Task }>> {
  const res = await apiClient.post('/alpha-agent/evolve', miningRequestBody(params), {
    headers: { 'Idempotency-Key': requestKey },
    signal,
  });
  return miningSubmissionResponse(res.data?.data ?? {}, params);
}

function miningSubmissionResponse(data: any, params: MiningStartParams): ApiResponse<{ taskId: string; task: Task }> {
  const taskId: string = data.task_id ?? '';
  if (!taskId) throw new Error('提交响应缺少任务编号，请重试确认原提交');
  const task = normalizeAgentTask(
    { ...data, task_id: taskId, status: data.status ?? 'queued' },
    {
      userInput: params.direction,
      algorithm: params.algorithm ?? 'rdagent',
      statisticalConfig: params.statisticalConfig,
      miningStart: params.miningStart,
      miningEnd: params.miningEnd,
      validStart: params.validStart,
      validEnd: params.validEnd,
      maxRounds: params.maxRounds ?? 3,
      universe: params.universe,
      datasets: params.datasets,
      freq: params.freq ?? 'day',
    },
  );
  return makeOk({ taskId, task });
}

export async function findMiningSubmission(
  params: MiningStartParams, requestKey: string, signal?: AbortSignal,
): Promise<ApiResponse<{ taskId: string; task: Task }> | null> {
  try {
    const res = await apiClient.get('/alpha-agent/submissions/by-request-key', { params: { request_key: requestKey }, signal });
    return miningSubmissionResponse(res.data?.data ?? {}, params);
  } catch (failure) {
    const status = (failure as { response?: { status?: number } }).response?.status;
    if (status === 404) return null;
    throw failure;
  }
}

export async function getMiningStatus(
  taskId: string,
): Promise<ApiResponse<{ task: Task }>> {
  const res = await apiClient.get(`/alpha-agent/tasks/${taskId}`);
  return makeOk({ task: normalizeAgentTask(res.data?.data) });
}

export async function cancelMining(
  taskId: string,
): Promise<ApiResponse<{ status: 'cancelled' | 'stopping' }>> {
  const res = await apiClient.post(`/alpha-agent/tasks/${taskId}/cancel`);
  return makeOk({ status: res.data?.data?.status ?? 'stopping' });
}

export async function listTasks(): Promise<ApiResponse<{ tasks: Task[] }>> {
  const res = await apiClient.get(`/alpha-agent/tasks`);
  const tasks: Task[] = (res.data?.data?.tasks ?? []).map((t: any) =>
    normalizeAgentTask(t),
  );
  return makeOk({ tasks });
}

export async function getTaskLog(
  taskId: string,
  offset = 0,
): Promise<{ lines: string[]; total: number }> {
  try {
    const res = await apiClient.get(
      `/alpha-agent/tasks/${taskId}/log?tail=500&offset=${offset}`,
    );
    const data = res.data?.data ?? {};
    return { lines: data.lines ?? [], total: data.total ?? 0 };
  } catch {
    return { lines: [], total: 0 };
  }
}

// ========================== Factor API ==========================

export interface FactorListParams {
  archived?: boolean;
  quality?: string;
  search?: string;
  limit?: number;
  offset?: number;
  library?: string;
  market?: string;
  universe?: string;
}

export interface FactorListResponse {
  factors: Factor[];
  total: number;
  limit: number;
  offset: number;
  metadata?: any;
  libraries?: string[];
  counts?: { total: number; high: number; medium: number; low: number };
  archiveSupported?: boolean;
}

export async function getFactors(
  params: FactorListParams = {},
  signal?: AbortSignal,
): Promise<ApiResponse<FactorListResponse>> {
  const qs = new URLSearchParams();
  // Backend caps `limit` at 200 — clamp client-side so callers requesting more
  // get the first 200 instead of a 422 validation error.
  const requested = params.limit ?? 200;
  const clamped = Math.min(Math.max(requested, 1), 200);
  qs.set('limit', String(clamped));
  qs.set('offset', String(Math.max(0, params.offset ?? 0)));
  if (params.market) qs.set('market', params.market);
  if (params.universe) qs.set('universe', params.universe);
  if (params.archived) qs.set('archived', 'true');
  if (params.search?.trim()) qs.set('search', params.search.trim());
  if (params.quality && params.quality !== 'all') qs.set('quality', params.quality);
  const res = await apiClient.get(`/alpha-agent/factors?${qs.toString()}`, { signal });
  const data = res.data?.data ?? {};
  const serverPagination = Number.isInteger(data.limit) && Number.isInteger(data.offset);
  let factors: Factor[] = (res.data?.data?.factors ?? []).map(normalizeAgentFactor);

  if (!serverPagination && params.quality) {
    factors = factors.filter((f) => f.quality === params.quality);
  }
  if (!serverPagination && params.search) {
    const s = params.search.toLowerCase();
    factors = factors.filter(
      (f) =>
        f.factorName.toLowerCase().includes(s) ||
        f.factorExpression.toLowerCase().includes(s) ||
        f.factorDescription.toLowerCase().includes(s),
    );
  }
  const total = serverPagination ? data.total : factors.length;
  const offset = serverPagination ? data.offset : (params.offset ?? 0);
  if (!serverPagination && params.limit) factors = factors.slice(offset, offset + params.limit);

  return makeOk({
    factors,
    total,
    limit: serverPagination ? data.limit : (params.limit ?? total),
    offset,
    counts: data.counts,
    archiveSupported: data.archive_supported === true,
    libraries: ['default'],
  });
}

export async function getFactorDetail(
  factorId: string,
  signal?: AbortSignal,
): Promise<ApiResponse<{ factor: any }>> {
  const res = await apiClient.get(`/alpha-agent/factors/${encodeURIComponent(factorId)}`, { signal });
  const raw = res.data?.data ?? {};
  return makeOk({ factor: { ...normalizeAgentFactor(raw), raw } });
}

/** 因子工厂产出的表达式因子（只读、共享，非某用户挖掘结果） */
export interface FactoryFactor {
  factorId: string;
  factorName: string;
  factorExpression: string;
  ic: number | null;
  icir: number | null;
  coverage: number | null;
  field: string;
}

export async function getFactoryFactors(signal?: AbortSignal): Promise<
  ApiResponse<{ factors: FactoryFactor[]; generatedAt: string | null }>
> {
  const res = await apiClient.get(`/alpha-agent/factory-factors`, { signal });
  const data = res.data?.data ?? {};
  const factors: FactoryFactor[] = (data.factors ?? []).map((raw: any) => ({
    factorId: raw.factor_id ?? '',
    factorName: raw.factor_name ?? 'unnamed',
    factorExpression: raw.factor_expression ?? raw.factor_formulation ?? '',
    ic: metricNumber(raw.ic_value),
    icir: metricNumber(raw.metadata?.icir ?? raw.icir),
    coverage: metricNumber(raw.metadata?.coverage ?? raw.coverage),
    field: raw.metadata?.field ?? '',
  }));
  return makeOk({ factors, generatedAt: data.generated_at ?? null });
}

export async function explainFactor(
  factorId: string,
  signal?: AbortSignal,
): Promise<ApiResponse<{ explanation: string; cached: boolean; source_revision_no?: number | null }>> {
  const data = await requestResearchResult<{ explanation: string; cached: boolean; source_revision_no?: number | null }>(`/alpha-agent/factors/${encodeURIComponent(factorId)}/explain`, undefined, signal);
  if (!data || typeof data.explanation !== 'string' || !data.explanation.trim()) throw new Error('因子解释响应无效');
  return makeOk(data);
}

export async function exportFactorToIde(
  factorId: string,
): Promise<ApiResponse<{ strategy_id: string; name: string; message: string }>> {
  const res = await apiClient.post(`/alpha-agent/factors/${factorId}/export`);
  return makeOk(res.data?.data ?? {});
}

export async function listFactorLibraries(): Promise<
  ApiResponse<{ libraries: string[] }>
> {
  try {
    const res = await apiClient.get(`/alpha-agent/factors`);
    const rawFactors: any[] = res.data?.data?.factors ?? [];
    // Extract unique factor IDs as library identifiers
    const libraries: string[] = rawFactors
      .map((f: any) => f.id ?? f.factor_id ?? '')
      .filter((id: string) => id.length > 0);
    return makeOk({ libraries });
  } catch {
    return makeOk({ libraries: [] });
  }
}

// ========================== QuantDB Data API ==========================

/** QuantDB data availability summary (date range, universes, datasets) */
export async function getDataSummary(): Promise<ApiResponse<DataSummary>> {
  try {
    const res = await apiClient.get(`/alpha-agent/data-summary`);
    const raw = res.data?.data ?? {};
    return makeOk({
      available: raw.available !== false,
      dateRange: raw.date_range
        ? {
            start: raw.date_range.start ?? '',
            end: raw.date_range.end ?? '',
            tradingDays: raw.date_range.trading_days ?? 0,
          }
        : undefined,
      universes: raw.universes ?? undefined,
      stockCount: raw.stock_count ?? undefined,
      datasetCount:
        typeof raw.dataset_count === 'number' ? raw.dataset_count : undefined,
      datasets: raw.datasets
        ? Object.fromEntries(
            Object.entries(raw.datasets).map(([name, info]: [string, any]) => [
              name,
              {
                columns: info?.columns ?? 0,
                categories: Array.isArray(info?.categories) ? info.categories : undefined,
                categoryCount: info?.category_count ?? info?.categoryCount ?? undefined,
              },
            ]),
          )
        : undefined,
      error: raw.error,
    });
  } catch (error: unknown) {
    const message = error instanceof Error ? error.message : '数据摘要获取失败';
    return makeOk({ available: false, error: message });
  }
}

/** L1 factor categories from QuantDB feature catalog */
export async function getFactorCategories(): Promise<
  ApiResponse<{ categories: FactorCategory[] }>
> {
  try {
    const res = await apiClient.get(`/alpha-agent/factor-categories`);
    const raw = res.data?.data?.categories ?? [];
    const categories: FactorCategory[] = raw.map((c: any) => ({
      id: c.id ?? '',
      name: c.name ?? '',
      featureCount: c.feature_count ?? 0,
      sampleFeatures: c.sample_features ?? [],
    }));
    return makeOk({ categories });
  } catch {
    return makeOk({ categories: [] });
  }
}

/** Available stock universes with constituent counts */
export async function getUniverses(): Promise<
  ApiResponse<{ universes: UniverseInfo[] }>
> {
  try {
    const res = await apiClient.get(`/alpha-agent/universes`);
    const raw = res.data?.data?.universes ?? {};
    const universes: UniverseInfo[] = Object.entries(raw).map(
      ([id, info]: [string, any]) => ({
        id: id as UniverseId,
        name: info?.name ?? UNIVERSE_LABELS[id as UniverseId] ?? id,
        indexSymbol: info?.indexSymbol ?? info?.index_symbol ?? null,
        stockCount: info?.count ?? 0,
        isSystem: info?.is_system ?? info?.isSystem ?? true,
      }),
    );
    return makeOk({ universes });
  } catch {
    // Fall back to the static label list so the selector still works offline
    const universes: UniverseInfo[] = (
      Object.keys(UNIVERSE_LABELS) as UniverseId[]
    ).map((id) => ({
      id,
      name: UNIVERSE_LABELS[id],
      indexSymbol: null,
      stockCount: 0,
    }));
    return makeOk({ universes });
  }
}

// ========================== Backtest API ==========================

export interface BacktestStartParams {
  factorId: string;
  factorSource?: string;
  configPath?: string;
  universe?: string;
  dataSource?: 'qlib_bin';
  /** 回测窗口起止（YYYY-MM-DD）；缺省后端默认近一年 */
  startDate?: string;
  endDate?: string;
}

export async function startBacktest(
  params: BacktestStartParams,
): Promise<ApiResponse<{ taskId: string; task: Task }>> {
  const factorId = params.factorId;
  if (!factorId) {
    return {
      success: false,
      error: '回测需要 factorId — 请在因子库中选择一个已生成的因子。',
    } as ApiResponse<any>;
  }
  const idempotencyKey = generateId();
  const res = await apiClient.post(
    '/alpha-agent/factor-jobs',
    {
      factor_id: factorId,
      kind: 'backtest',
      ...(params.universe ? { universe: params.universe } : {}),
      ...(params.startDate ? { start_date: params.startDate } : {}),
      ...(params.endDate ? { end_date: params.endDate } : {}),
      data_source: params.dataSource ?? 'qlib_bin',
    },
    { headers: { 'Idempotency-Key': idempotencyKey } },
  );
  const data = res.data?.data ?? {};
  const taskId = data.job_id;
  if (typeof taskId !== 'string' || !taskId) {
    return { success: false, error: '后端没有返回有效的回测作业 ID。' } as ApiResponse<any>;
  }
  return makeOk({
    taskId,
    task: normalizeAgentTask({
      task_id: taskId,
      status: data.status ?? 'queued',
      progress: data.progress_snapshot?.message ?? '已加入回测队列',
    }),
  });
}

export async function getBacktestStatus(
  taskId: string,
): Promise<ApiResponse<{ task: Task }>> {
  try {
    const res = await apiClient.get(`/alpha-agent/factor-jobs/${encodeURIComponent(taskId)}`);
    const raw = res.data?.data ?? {};
    const sourceMetrics = raw.result_snapshot?.metrics ?? {};
    const metrics: Record<string, any> = {};
    if (sourceMetrics.ic_mean != null) metrics.ic = sourceMetrics.ic_mean;
    if (sourceMetrics.rank_ic_median != null) metrics.rankIc = sourceMetrics.rank_ic_median;
    if (sourceMetrics.icir != null) metrics.icir = sourceMetrics.icir;
    if (sourceMetrics.sharpe_ratio != null) metrics.sharpeRatio = sourceMetrics.sharpe_ratio;
    if (sourceMetrics.annual_return != null) metrics.annualReturn = sourceMetrics.annual_return;
    if (sourceMetrics.max_drawdown != null) metrics.maxDrawdown = sourceMetrics.max_drawdown;
    return makeOk({
      task: normalizeAgentTask({
        task_id: taskId,
        status: raw.status ?? 'failed',
        progress: raw.progress_snapshot?.message ?? raw.error_message,
        metrics: Object.keys(metrics).length > 0 ? metrics : undefined,
      }),
    });
  } catch {
    return makeOk({ task: normalizeAgentTask({ task_id: taskId, status: 'failed' }) });
  }
}

export async function cancelBacktest(taskId: string): Promise<ApiResponse> {
  const res = await apiClient.post(
    `/alpha-agent/factor-jobs/${encodeURIComponent(taskId)}/cancel`,
  );
  return makeOk(res.data?.data ?? {});
}

// ========================== LLM Config ==========================

export interface LlmConfigStatus {
  configured: boolean;
  reason?: string;
  /** 配置来源：系统统一维护的服务器环境变量。 */
  source?: 'env';
  provider?: string;
  model?: string;
  base_url?: string;
  api_key_masked?: string;
}

/** Read-only LLM config status from backend (key resolved from env vars). */
export async function getLlmConfig(): Promise<ApiResponse<LlmConfigStatus>> {
  try {
    const res = await apiClient.get(`/alpha-agent/llm-config`);
    return makeOk(res.data?.data ?? { configured: false, reason: '未知状态' });
  } catch (error: unknown) {
    const message = error instanceof Error ? error.message : 'LLM 配置查询失败';
    return makeOk({ configured: false, reason: message });
  }
}

// ========================== Health Check ==========================

export async function healthCheck(): Promise<
  ApiResponse<{ status: string; timestamp: string }>
> {
  await apiClient.get(`/alpha-agent/stats`);
  return makeOk({ status: 'ok', timestamp: new Date().toISOString() });
}

// ========================== Pseudo WebSocket via polling ==========================

export type WsCallback = (msg: WsMessage) => void;

/**
 * frontend-v2 expects a WebSocket lifecycle. AlphaAgent has no WS, so we poll
 * /alpha-agent/tasks/:id and /alpha-agent/tasks/:id/log to synthesize
 * progress/log/result messages with rich detail.
 */
export function connectMiningWs(
  taskId: string,
  onMessage: WsCallback,
  onClose?: () => void,
  _onError?: (e: Event) => void,
): WebSocket {
  let stopped = false;
  let lastStatus = '';
  const fakeWs: any = {
    readyState: 1,
    send: (_data: string) => {},
    close: () => {
      stopped = true;
      logStream?.close();
      logStream = null;
      fakeWs.readyState = 3;
      onClose?.();
    },
    /** Exposed so callers can clear the recursive setTimeout on unmount */
    _pollingTimeoutId: null as ReturnType<typeof setTimeout> | null,
  };

  let lastPhase = '';
  let lastPct = -1;
  let lastFactorsCount = -1;
  let logOffset = 0;
  let logBootstrapped = false;

  // Regex to parse RD-Agent log lines like:
  // 2026-05-31 00:30:41,967 [INFO] LiteLLM: completion() model=...
  // 2026-05-31 00:30:41.967 | INFO     | rdagent.module: message
  const LOG_LINE_RE = /^(\d{4}-\d{2}-\d{2}[\sT]\d{2}:\d{2}:\d{2})[,.]?\d*\s*(?:\[|\|)\s*(\w+)/;

  function classifyLogLine(line: string): 'info' | 'warning' | 'error' | 'success' {
    const m = line.match(LOG_LINE_RE);
    if (m) {
      const level = m[2].toUpperCase();
      if (level === 'ERROR' || level === 'CRITICAL') return 'error';
      if (level === 'WARNING' || level === 'WARN') return 'warning';
    }
    if (line.includes('FileNotFoundError') || line.includes('Error') || line.includes('FAILED') || line.includes('Traceback')) return 'error';
    if (line.includes('success') || line.includes('Persisted') || line.includes('completed')) return 'success';
    if (line.includes('WARNING') || line.includes('warning')) return 'warning';
    return 'info';
  }

  function extractTimestamp(line: string): string {
    const m = line.match(/^(\d{4}-\d{2}-\d{2}[\sT]\d{2}:\d{2}:\d{2})/);
    if (m) {
      return m[1].replace(' ', 'T') + (m[1].includes('T') ? '' : ':00');
    }
    return new Date().toISOString();
  }

  /** Filter and format log lines for display — skip noisy/repetitive lines */
  function filterLogLine(line: string): string | null {
    const trimmed = line.trim();
    if (!trimmed) return null;
    // Skip ANSI escape codes
    const escapeCode = new RegExp(`${String.fromCharCode(27)}\\[[0-9;]*m`, 'g');
    const clean = trimmed.replace(escapeCode, '').replace(/\[0m/g, '');
    // Skip very short lines (just timestamps, brackets)
    if (clean.length < 10) return null;
    // Skip repetitive prompt template lines
    if (clean.startsWith('# daily_pv.parquet')) return null;
    if (clean.startsWith('## File Type')) return null;
    if (clean.startsWith('## Content Overview')) return null;
    if (clean.startsWith('#### All Columns:')) return null;
    if (clean.startsWith('#### $')) return null;
    if (clean.startsWith('One possible format')) return null;
    if (clean.startsWith('The result file should')) return null;
    if (clean.startsWith('User will write your python')) return null;
    if (clean.startsWith('The user will provide')) return null;
    if (clean.startsWith('Please generate the output')) return null;
    if (clean.startsWith('The output should follow JSON')) return null;
    if (clean === '```' || clean === '```json') return null;
    // Keep everything else
    return clean;
  }

  const emitLogLines = (lines: string[]) => {
    for (const rawLine of lines) {
      const filtered = filterLogLine(rawLine);
      if (!filtered) continue;
      onMessage({
        type: 'log',
        taskId,
        data: {
          id: `${taskId}-log-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
          timestamp: extractTimestamp(rawLine),
          level: classifyLogLine(rawLine),
          message: filtered,
        },
        timestamp: new Date().toISOString(),
      });
    }
  };

  let logStream: { close: () => void } | null = null;
  let logStreamDenied = false;
  try {
    logStream = openMiningLogStream(taskId, {
      onLines: (lines) => {
        if (!stopped) emitLogLines(lines);
      },
      onDone: () => {},
      onError: () => {
        // 代理或网络不支持流式：退回增量轮询，保证历史不丢。
        logStreamDenied = true;
        logStream?.close();
        logStream = null;
      },
    });
  } catch {
    logStream = null;
  }
  if (!logStream) logStreamDenied = true;

  const poll = async () => {
    if (stopped) return;
    try {
      // 1. Poll task status
      const res = await apiClient.get(`/alpha-agent/tasks/${taskId}`);
      const data = res.data?.data ?? {};
      const status: string = data.status ?? '';
      const backendPhase: string = typeof data.phase === 'string' ? data.phase : '';
      const progressText =
        typeof data.progress === 'string' ? data.progress : '';
      const progressPct: number =
        typeof data.progress_pct === 'number'
          ? data.progress_pct
          : status === 'completed'
            ? 100
            : status === 'running'
              ? 5
              : 0;
      const currentRound =
        typeof data.current_loop === 'number' ? data.current_loop : 0;
      const totalRounds =
        typeof data.loop_n === 'number' ? data.loop_n : 0;

      const phaseChanged = backendPhase !== lastPhase;
      const pctChanged = progressPct !== lastPct;
      const statusChanged = status !== lastStatus;
      // 后端随任务状态返回的结构化因子（rd_agent_factors 已落库），优先于日志正则解析
      const backendFactors: any[] = Array.isArray(data.factors) ? data.factors : [];
      const factorsChanged = backendFactors.length !== lastFactorsCount;

      if (statusChanged || phaseChanged || pctChanged || factorsChanged) {
        lastStatus = status;
        lastPhase = backendPhase;
        lastPct = progressPct;
        lastFactorsCount = backendFactors.length;
        const phase: ExecutionPhase =
          status === 'completed'
            ? 'completed'
            : PHASE_MAP[backendPhase] || 'parsing';

        onMessage({
          type: 'progress',
          taskId,
          data: {
            phase,
            currentRound,
            totalRounds,
            progress: progressPct,
            message: progressText || status,
            status: normalizeTaskStatus(status),
            queuePosition: data.queue_position,
            timestamp: new Date().toISOString(),
            timeline: data.timeline ?? undefined,
            tokenUsage: data.token_usage ?? undefined,
            factors: backendFactors.length > 0 ? backendFactors : undefined,
          },
          timestamp: new Date().toISOString(),
        });

        if (status === 'completed' || status === 'failed' || status === 'cancelled') {
          onMessage({
            type: 'result',
            taskId,
            data: { status },
            timestamp: new Date().toISOString(),
          });
          stopped = true;
          logStream?.close();
          logStream = null;
          fakeWs.readyState = 3;
          onClose?.();
          return;
        }
      }

      // 2. Poll detailed subprocess logs (rich output)
      //    SSE 可用时完全跳过这段，避免重复拉取与读放大。
      if (logStreamDenied) try {
        const fetchLog = async (offset: number) => {
          const logRes = await apiClient.get(
            `/alpha-agent/tasks/${taskId}/log?tail=500&offset=${offset}`,
          );
          return logRes.data?.data ?? {};
        };
        let logData = await fetchLog(logOffset);
        let lines: string[] = logData.lines ?? [];
        if (!logBootstrapped) {
          // 首次绑定（含刷新后恢复）直接看最新 500 行，而不是从第 0 行
          // 以每 2 秒 500 行的速度补历史，否则日志会长时间落后于实际进度。
          logBootstrapped = true;
          const total = Number(logData.total ?? lines.length);
          if (total > 500 && lines.length < total) {
            logData = await fetchLog(total - 500);
            lines = logData.lines ?? [];
          }
          // 只把本次返回的行计入下方累加，最终对齐到 total
          logOffset = total - lines.length;
        }
        if (lines.length > 0) {
          logOffset += lines.length;
          emitLogLines(lines);
        }
      } catch {
        /* log endpoint may not exist yet — ignore */
      }
    } catch {
      /* transient — keep polling */
    }
    if (!stopped) {
      fakeWs._pollingTimeoutId = setTimeout(poll, 2000);
    }
  };

  fakeWs._pollingTimeoutId = setTimeout(poll, 100);
  return fakeWs as WebSocket;
}

// ========================== SSE log stream ==========================

export interface MiningLogStreamHandle {
  close: () => void;
}

/**
 * 订阅挖掘任务日志（SSE，服务端按字节偏移增量读取，文件仍是事实源）。
 *
 * 返回 null 表示当前环境不支持流式，调用方应退回增量轮询。
 */
export function openMiningLogStream(
  taskId: string,
  handlers: {
    onLines: (lines: string[]) => void;
    onDone: () => void;
    onError: () => void;
  },
): MiningLogStreamHandle | null {
  if (typeof fetch !== 'function' || typeof AbortController !== 'function') {
    return null;
  }
  const base = String(SERVICE_ENDPOINTS.AI_STRATEGY || '').replace(/\/+$/, '');
  const url = `${base}/alpha-agent/tasks/${encodeURIComponent(taskId)}/log/stream?tail_lines=200`;
  const controller = new AbortController();
  let closed = false;
  const token = authService.getAccessToken();
  const headers: Record<string, string> = { Accept: 'text/event-stream' };
  if (token) headers.Authorization = `Bearer ${token}`;

  (async () => {
    try {
      const response = await fetch(url, {
        method: 'GET',
        headers,
        signal: controller.signal,
        cache: 'no-store',
      });
      if (!response.ok || !response.body) {
        if (!closed) handlers.onError();
        return;
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done || closed) break;
        buffer += decoder.decode(value, { stream: true });
        const frames = buffer.split('\n\n');
        buffer = frames.pop() ?? '';
        for (const frame of frames) {
          const payload = frame
            .split('\n')
            .filter((line) => line.startsWith('data:'))
            .map((line) => line.slice(5).trim())
            .join('');
          if (!payload) continue;
          try {
            const parsed = JSON.parse(payload) as {
              lines?: unknown;
              done?: unknown;
            };
            if (Array.isArray(parsed.lines)) {
              handlers.onLines(parsed.lines.map((line) => String(line)));
            }
            if (parsed.done === true) handlers.onDone();
          } catch {
            // 心跳或无法解析的帧直接忽略
          }
        }
      }
    } catch {
      if (!closed) handlers.onError();
    }
  })();

  return {
    close: () => {
      closed = true;
      controller.abort();
    },
  };
}
