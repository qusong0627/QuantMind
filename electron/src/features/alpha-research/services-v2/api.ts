/**
 * QuantaAlpha frontend-v2 API bridge
 *
 * The original frontend-v2 expected its own dedicated FastAPI backend with
 * endpoints like /api/v1/mining/start, /api/v1/factors, WS /ws/mining/{id}.
 * In QuantMind we expose AlphaAgent under /api/v1/alpha-agent/* via the engine
 * service. This module preserves the original public surface (signatures,
 * return shapes) so the ported pages/components compile and run, but delegates
 * to the QuantMind alpha-agent router and normalizes the response shape.
 */

import { apiClient } from '../../../services/aiStrategyClients';
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
    ic: 0,
    icir: 0,
    rankIc: 0,
    rankIcir: 0,
    annualReturn: 0,
    sharpeRatio: 0,
    maxDrawdown: 0,
    totalFactors: 0,
    highQualityFactors: 0,
    mediumQualityFactors: 0,
    lowQualityFactors: 0,
    top10Factors: [],
  };
}

export function classifyQuality(ic: number | null | undefined): 'high' | 'medium' | 'low' {
  if (ic == null) return 'low';
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
    case 'cancelled':
      return 'failed';
    case 'running':
    case 'pending':
      return 'running';
    default:
      return 'idle';
  }
}

const PHASE_MAP: Record<string, ExecutionPhase> = {
  pending: 'parsing',
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
    status,
    config: configHint ?? { userInput: '' },
    progress: {
      phase,
      currentRound,
      totalRounds,
      progress: progressNum,
      message:
        typeof raw?.progress === 'string'
          ? raw.progress
          : raw?.error_message || (status === 'completed' ? '完成' : '运行中'),
      timestamp: raw?.updated_at ?? new Date().toISOString(),
    },
    // 回测任务由 getBacktestStatus 把后端 factor 详情里的指标放进 raw.metrics；
    // 此前这里无条件用 emptyMetrics() 覆盖，导致「回测结果」面板永远显示 0.0000 / --。
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

function normalizeAgentFactor(raw: any): Factor {
  const ic = raw?.ic_value ?? null;
  const meta = raw?.metadata ?? {};
  return {
    factorId: raw?.id ?? raw?.factor_id ?? '',
    factorName: raw?.factor_name ?? 'unnamed',
    factorExpression: raw?.factor_formulation ?? raw?.factor_code ?? '',
    factorDescription: meta.description ?? raw?.category ?? '',
    quality: classifyQuality(ic),
    market: meta.market ?? raw?.market ?? undefined,
    universe: raw?.universe ?? meta.universe ?? undefined,
    ic: ic ?? 0,
    icir: meta.icir ?? 0,
    rankIc: raw?.rank_ic ?? meta.rank_ic ?? 0,
    rankIcir: 0,
    sharpeRatio: raw?.sharpe_ratio ?? 0,
    annualReturn: raw?.annual_return ?? 0,
    maxDrawdown: raw?.max_drawdown ?? 0,
    round: meta.round ?? 0,
    direction: meta.direction ?? raw?.category ?? '',
    createdAt: raw?.created_at ?? '',
  };
}

// ========================== Mining API ==========================

export interface MiningStartParams {
  direction: string;
  market?: string;
  universe?: string;
  dataSource?: string;
  numDirections?: number;
  maxRounds?: number;
  maxLoops?: number;
  factorsPerHypothesis?: number;
  librarySuffix?: string;
  qualityGateEnabled?: boolean;
  parallelEnabled?: boolean;
  /** L1 因子类别方向（多选，label） */
  directions?: string[];
  /** 类别选择模式：selected=取第一条，random=随机一条 */
  directionMode?: 'selected' | 'random';
}

export async function startMining(
  params: MiningStartParams,
): Promise<ApiResponse<{ taskId: string; task: Task }>> {
  const loopN = params.maxRounds ?? params.maxLoops ?? 3;
  const qs = new URLSearchParams({
    loop_n: String(loopN),
    direction: params.direction || '',
  });
  if (params.market) qs.set('market', params.market);
  if (params.universe) qs.set('universe', params.universe);
  if (params.dataSource) qs.set('data_source', params.dataSource);
  for (const d of params.directions ?? []) {
    if (d && d.trim()) qs.append('directions', d.trim());
  }
  if (params.directionMode) qs.set('direction_mode', params.directionMode);
  const res = await apiClient.post(`/alpha-agent/evolve?${qs.toString()}`);
  const data = res.data?.data ?? {};
  const taskId: string = data.task_id ?? '';
  const task = normalizeAgentTask(
    { task_id: taskId, status: data.status ?? 'pending' },
    {
      userInput: params.direction,
      numDirections: params.numDirections,
      maxRounds: loopN,
      universe: params.universe,
      librarySuffix: params.librarySuffix,
      qualityGateEnabled: params.qualityGateEnabled,
      parallelExecution: params.parallelEnabled,
    },
  );
  return makeOk({ taskId, task });
}

export async function getMiningStatus(
  taskId: string,
): Promise<ApiResponse<{ task: Task }>> {
  const res = await apiClient.get(`/alpha-agent/tasks/${taskId}`);
  return makeOk({ task: normalizeAgentTask(res.data?.data) });
}

export async function cancelMining(taskId: string): Promise<ApiResponse> {
  await apiClient.post(`/alpha-agent/tasks/${taskId}/cancel`);
  return makeOk({});
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
}

export async function getFactors(
  params: FactorListParams = {},
): Promise<ApiResponse<FactorListResponse>> {
  const qs = new URLSearchParams();
  // Backend caps `limit` at 200 — clamp client-side so callers requesting more
  // get the first 200 instead of a 422 validation error.
  const requested = params.limit ?? 200;
  const clamped = Math.min(Math.max(requested, 1), 200);
  qs.set('limit', String(clamped));
  if (params.market) qs.set('market', params.market);
  if (params.universe) qs.set('universe', params.universe);
  const res = await apiClient.get(`/alpha-agent/factors?${qs.toString()}`);
  let factors: Factor[] = (res.data?.data?.factors ?? []).map(normalizeAgentFactor);

  if (params.quality) {
    factors = factors.filter((f) => f.quality === params.quality);
  }
  if (params.search) {
    const s = params.search.toLowerCase();
    factors = factors.filter(
      (f) =>
        f.factorName.toLowerCase().includes(s) ||
        f.factorExpression.toLowerCase().includes(s) ||
        f.factorDescription.toLowerCase().includes(s),
    );
  }
  const total = factors.length;
  const offset = params.offset ?? 0;
  if (params.limit) factors = factors.slice(offset, offset + params.limit);

  return makeOk({
    factors,
    total,
    limit: params.limit ?? total,
    offset,
    libraries: ['default'],
  });
}

export async function getFactorDetail(
  factorId: string,
): Promise<ApiResponse<{ factor: any }>> {
  const res = await apiClient.get(`/alpha-agent/factors/${factorId}`);
  const raw = res.data?.data ?? {};
  return makeOk({ factor: { ...normalizeAgentFactor(raw), raw } });
}

/** 因子工厂产出的表达式因子（只读、共享，非某用户挖掘结果） */
export interface FactoryFactor {
  factorId: string;
  factorName: string;
  factorExpression: string;
  ic: number;
  icir: number;
  coverage: number;
  field: string;
}

export async function getFactoryFactors(): Promise<
  ApiResponse<{ factors: FactoryFactor[]; generatedAt: string | null }>
> {
  const res = await apiClient.get(`/alpha-agent/factory-factors`);
  const data = res.data?.data ?? {};
  const factors: FactoryFactor[] = (data.factors ?? []).map((raw: any) => ({
    factorId: raw.factor_id ?? '',
    factorName: raw.factor_name ?? 'unnamed',
    factorExpression: raw.factor_expression ?? raw.factor_formulation ?? '',
    ic: raw.ic_value ?? 0,
    icir: raw.metadata?.icir ?? raw.icir ?? 0,
    coverage: raw.metadata?.coverage ?? raw.coverage ?? 0,
    field: raw.metadata?.field ?? '',
  }));
  return makeOk({ factors, generatedAt: data.generated_at ?? null });
}

export async function explainFactor(
  factorId: string,
): Promise<ApiResponse<{ explanation: string; cached: boolean }>> {
  const res = await apiClient.post(`/alpha-agent/factors/${factorId}/explain`);
  return makeOk(res.data?.data ?? { explanation: '', cached: false });
}

export async function exportFactorToIde(
  factorId: string,
): Promise<ApiResponse<{ strategy_id: string; name: string; message: string }>> {
  const res = await apiClient.post(`/alpha-agent/factors/${factorId}/export`);
  return makeOk(res.data?.data ?? {});
}

/** 回测可选的因子项（下拉显示 name，提交用 id） */
export interface FactorLibraryOption {
  id: string;
  name: string;
  ic: number | null;
  status: string;
}

export async function listFactorLibraries(): Promise<
  ApiResponse<{ libraries: FactorLibraryOption[] }>
> {
  try {
    const res = await apiClient.get(`/alpha-agent/factors?limit=200`);
    const rawFactors: any[] = res.data?.data?.factors ?? [];
    // 下拉项以因子为单位：显示 factor_name，值用 factor_id。
    // 此前只回显裸 id（如 1897d59bf8cc143d3c339b5f105d2efd），用户无法判断是哪个因子。
    const libraries: FactorLibraryOption[] = rawFactors
      .map((f: any) => ({
        id: String(f.factor_id ?? f.id ?? ''),
        name: String(f.factor_name ?? f.name ?? ''),
        ic: typeof f.ic_value === 'number' ? f.ic_value : null,
        status: String(f.status ?? ''),
      }))
      .filter((o: FactorLibraryOption) => o.id.length > 0);
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
  dataSource?: 'qlib_bin' | 'h5';
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
  const qs = new URLSearchParams();
  if (params.universe) qs.set('universe', params.universe);
  if (params.dataSource) qs.set('data_source', params.dataSource);
  if (params.startDate) qs.set('start_date', params.startDate);
  if (params.endDate) qs.set('end_date', params.endDate);
  const query = qs.toString();
  const res = await apiClient.post(
    `/alpha-agent/factors/${factorId}/backtest${query ? `?${query}` : ''}`,
  );
  const data = res.data?.data ?? {};
  const taskId = data.factor_id ?? factorId;
  return makeOk({
    taskId,
    task: normalizeAgentTask({
      task_id: taskId,
      status: data.status ?? 'running',
      progress: data.message,
    }),
  });
}

export async function getBacktestStatus(
  taskId: string,
): Promise<ApiResponse<{ task: Task }>> {
  try {
    const res = await apiClient.get(`/alpha-agent/factors/${taskId}`);
    const raw = res.data?.data ?? {};
    // Map backend status correctly: 'failed' should map to 'failed', not 'running'
    const rawStatus: string = raw.status ?? '';
    const status: string =
      rawStatus === 'completed' ? 'completed' :
      rawStatus === 'failed' || rawStatus === 'cancelled' ? 'failed' :
      'running';
    // Extract metrics from factor detail response
    const metrics: Record<string, any> = {};
    if (raw.ic_value != null) metrics.ic = raw.ic_value;
    if (raw.sharpe_ratio != null) metrics.sharpeRatio = raw.sharpe_ratio;
    if (raw.annual_return != null) metrics.annualReturn = raw.annual_return;
    if (raw.max_drawdown != null) metrics.maxDrawdown = raw.max_drawdown;
    if (raw.rank_ic != null) metrics.rankIc = raw.rank_ic;
    // ICIR / Rank ICIR 不是表字段，落在 metadata_json（挖掘阶段写入 icir / rank_icir）
    const meta = raw.metadata ?? {};
    const asNum = (v: any): number | null => {
      if (typeof v === 'number') return v;
      if (typeof v === 'string' && v.trim() !== '' && !Number.isNaN(Number(v))) return Number(v);
      return null;
    };
    const icir = asNum(meta.icir);
    const rankIcir = asNum(meta.rank_icir);
    if (icir != null) metrics.icir = icir;
    if (rankIcir != null) metrics.rankIcir = rankIcir;
    return makeOk({
      task: normalizeAgentTask({
        task_id: taskId,
        status,
        progress: status === 'completed' ? 'Backtest done' : 'Running',
        metrics: Object.keys(metrics).length > 0 ? metrics : undefined,
      }),
    });
  } catch {
    return makeOk({ task: normalizeAgentTask({ task_id: taskId, status: 'failed' }) });
  }
}

export async function cancelBacktest(_taskId: string): Promise<ApiResponse> {
  return makeOk({});
}

// ========================== LLM Config ==========================

export interface LlmConfigStatus {
  configured: boolean;
  reason?: string;
  /** 配置来源：env=服务器环境变量，user_profile=个人中心 AI 服务配置 */
  source?: 'env' | 'user_profile';
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
    const clean = trimmed.replace(/\x1b\[[0-9;]*m/g, '').replace(/\[0m/g, '');
    // Skip very short lines (just timestamps, brackets)
    if (clean.length < 10) return null;
    // Skip repetitive prompt template lines
    if (clean.startsWith('# daily_pv.h5')) return null;
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
            data: { status: status === 'completed' ? 'completed' : 'failed' },
            timestamp: new Date().toISOString(),
          });
          stopped = true;
          fakeWs.readyState = 3;
          onClose?.();
          return;
        }
      }

      // 2. Poll detailed subprocess logs (rich output)
      try {
        const logRes = await apiClient.get(
          `/alpha-agent/tasks/${taskId}/log?tail=500&offset=${logOffset}`,
        );
        const logData = logRes.data?.data ?? {};
        const lines: string[] = logData.lines ?? [];
        if (lines.length > 0) {
          logOffset += lines.length;
          for (const rawLine of lines) {
            const filtered = filterLogLine(rawLine);
            if (!filtered) continue;
            onMessage({
              type: 'log',
              taskId,
              data: {
                id: `${taskId}-log-${logOffset}-${Math.random().toString(36).slice(2, 6)}`,
                timestamp: extractTimestamp(rawLine),
                level: classifyLogLine(rawLine),
                message: filtered,
              },
              timestamp: new Date().toISOString(),
            });
          }
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
