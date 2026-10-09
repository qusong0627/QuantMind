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

export function emptyMetrics(): RealtimeMetrics {
  // 计数 + 空清单；IC/收益族一律缺席（undefined）——界面显「—」，
  // 绝不把「没算过」伪造成 0.0000
  return {
    totalFactors: 0,
    highQualityFactors: 0,
    mediumQualityFactors: 0,
    lowQualityFactors: 0,
    factors: [],
  };
}

export function classifyQuality(
  ic: number | null | undefined,
): 'high' | 'medium' | 'low' | 'unknown' {
  // IC 缺失 = 质量无从分级；旧实现判 'low'（缺失被当成差因子）
  if (ic == null || !Number.isFinite(ic)) return 'unknown';
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
    case 'queued':
      // 批量派发的排队态是一等状态（有「第 N 位」语义），不并进 running
      return 'queued';
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

export function normalizeAgentTask(raw: any, configHint?: any): Task {
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
  const queuePosition =
    typeof raw?.queue_position === 'number' ? raw.queue_position : null;

  return {
    taskId: raw?.task_id ?? raw?.taskId ?? '',
    status,
    // configHint 优先（提交瞬间的意图）；否则用后端返回的 direction——
    // /tasks 与 /tasks/{id} 现在都带它，监视器与恢复的任务行据此显示方向摘要。
    config: configHint ?? { userInput: raw?.direction ?? '' },
    progress: {
      phase,
      currentRound,
      totalRounds,
      progress: progressNum,
      message:
        // 排队态后端 progress 为空串——文案归一到中文「排队中（第 N 位）」
        status === 'queued'
          ? `排队中${queuePosition ? `（第 ${queuePosition} 位）` : ''}`
          : typeof raw?.progress === 'string'
            ? raw.progress
            : raw?.error_message || (status === 'completed' ? '完成' : '运行中'),
      timestamp: raw?.updated_at ?? new Date().toISOString(),
    },
    queuePosition,
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

/** 宽松数值归一（number 原样 / 数字字符串转换 / 其余 null）。缺失绝不补 0。 */
function toNumber(value: any): number | null {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null;
  if (typeof value === 'string' && value.trim() !== '' && !Number.isNaN(Number(value))) {
    return Number(value);
  }
  return null;
}

/** 从多个候选源里取第一个有效数值（表字段 → metadata 的优先级由调用方给出）。 */
function pickNumber(...candidates: any[]): number | undefined {
  for (const candidate of candidates) {
    const value = toNumber(candidate);
    if (value != null) return value;
  }
  return undefined;
}

/** 物化条目（后端 snake_case）→ 前端 camelCase；无条目返回 undefined。 */
function normalizeMaterialization(meta: any): Factor['materialization'] {
  const raw = meta?.materialization;
  if (!raw || typeof raw !== 'object' || typeof raw.status !== 'string') return undefined;
  return {
    status: raw.status,
    column: typeof raw.column === 'string' ? raw.column : undefined,
    name: typeof raw.name === 'string' ? raw.name : undefined,
    values: toNumber(raw.values) ?? undefined,
    corr: toNumber(raw.corr),
    corrAgainst: typeof raw.corr_against === 'string' ? raw.corr_against : null,
    at: typeof raw.at === 'string' ? raw.at : undefined,
    gates: raw.gates,
    error: typeof raw.error === 'string' ? raw.error : undefined,
  };
}

export function normalizeAgentFactor(raw: any): Factor {
  const ic = raw?.ic_value ?? null;
  const meta = raw?.metadata ?? {};
  const quality = meta.quality ?? {};
  const pfsQuality =
    pickNumber(quality.pfs, quality.pfs_gauss, quality.pfs_t, quality.n_days) != null
      ? {
          pfs: pickNumber(quality.pfs),
          pfsGauss: pickNumber(quality.pfs_gauss),
          pfsT: pickNumber(quality.pfs_t),
          nDays: pickNumber(quality.n_days),
        }
      : undefined;
  return {
    factorId: raw?.id ?? raw?.factor_id ?? '',
    factorName: raw?.factor_name ?? 'unnamed',
    factorExpression: raw?.factor_formulation ?? raw?.factor_code ?? '',
    factorDescription: meta.description ?? raw?.category ?? '',
    quality: classifyQuality(ic),
    market: meta.market ?? raw?.market ?? undefined,
    universe: raw?.universe ?? meta.universe ?? undefined,
    // 指标族一律「没有真实值就 undefined」——两个回测路径（表字段 / metadata 双写）
    // 都认；旧实现对缺失硬编码 0，把「没算过」伪装成「算出来是 0」。
    ic: pickNumber(ic),
    icir: pickNumber(raw?.icir, meta.icir),
    rankIc: pickNumber(raw?.rank_ic, meta.rank_ic),
    rankIcir: pickNumber(raw?.rank_icir, meta.rank_icir),
    sharpeRatio: pickNumber(raw?.sharpe_ratio, meta.sharpe_ratio),
    annualReturn: pickNumber(raw?.annual_return, meta.annual_return),
    maxDrawdown: pickNumber(raw?.max_drawdown, meta.max_drawdown),
    // 机构级指标（mining_plugins 评估器链）：缺失保持 undefined → 界面显「—」
    rre: pickNumber(meta.rre),
    pfsQuality,
    turnoverDaily: pickNumber(meta.turnover_daily),
    annTurnover: pickNumber(meta.ann_turnover),
    annReturnNet: pickNumber(meta.ann_return_net),
    sharpeNet: pickNumber(meta.sharpe_net),
    maxDrawdownNet: pickNumber(meta.max_drawdown_net),
    nObs: pickNumber(meta.n_obs),
    // 物化状态（metadata.materialization，物化器回写；未物化 = 缺席）
    materialization: normalizeMaterialization(meta),
    // 历史因子（user_id IS NULL）：只读。服务端 for_write 仍兜底 404，这里只做 UI 预判
    ownerless: raw?.user_id == null,
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
  /**
   * 文档血统：来自文档链的挖掘带上它。**带 docId 时改走 JSON body 变体**
   * （后端 EvolveRequest.doc_id → 落任务 source=doc + 回写文档 task_id）；
   * 不带时 query 形态一字不动（老路径零回归）。
   */
  docId?: string;
}

export async function startMining(
  params: MiningStartParams,
): Promise<ApiResponse<{ taskId: string; task: Task }>> {
  const loopN = params.maxRounds ?? params.maxLoops ?? 3;
  let res: { data?: { data?: { task_id?: string; status?: string } } };
  if (params.docId) {
    // JSON body 变体（与后端 EvolveRequest 字段对齐）；query 路径保持原样
    res = await apiClient.post('/alpha-agent/evolve', {
      direction: params.direction || '',
      market: params.market || 'a_share',
      universe: params.universe || 'csi300',
      data_source: params.dataSource || '',
      loop_n: loopN,
      directions: params.directions ?? [],
      direction_mode: params.directionMode || 'selected',
      doc_id: params.docId,
    });
  } else {
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
    res = await apiClient.post(`/alpha-agent/evolve?${qs.toString()}`);
  }
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

// ========================== 方向拆解 & 批量派发 ==========================

/** 拆解卡片（后端 validate_cards 归一后的形状；可选字段为空则缺席） */
export interface DecomposeCard {
  title: string;
  hypothesis: string;
  rationale?: string;
  categories: string[];
  evaluation_hint?: string;
}

export interface DecomposeResult {
  promptVersion: string;
  cards: DecomposeCard[];
  /** 超出卡片数上限被截断的数量（如实上报，不静默丢） */
  dropped: number;
  maxCards: number;
  context: {
    categories: number;
    poolDigestChars: number;
    poolFactors: number;
    model?: string | null;
  };
}

export interface DecomposeParams {
  direction: string;
  market?: string;
  universe?: string;
  maxCards?: number;
}

/**
 * 粗方向 → 正交子假设卡片（只拆解，不落任务）。
 * 失败（无 LLM 配置 412 / 超长或截断 400）由调用方 catch，
 * `err.response.data.detail` 是可直接上屏的中文文案。
 */
export async function decomposeDirection(
  params: DecomposeParams,
): Promise<ApiResponse<DecomposeResult>> {
  const res = await apiClient.post('/alpha-agent/directions/decompose', {
    direction: params.direction,
    market: params.market || 'a_share',
    universe: params.universe || 'csi300',
    ...(params.maxCards ? { max_cards: params.maxCards } : {}),
  });
  const data = res.data?.data ?? {};
  const cards: DecomposeCard[] = (data.cards ?? []).map((c: any) => ({
    title: c?.title ?? '',
    hypothesis: c?.hypothesis ?? '',
    ...(c?.rationale ? { rationale: c.rationale } : {}),
    categories: Array.isArray(c?.categories) ? c.categories : [],
    ...(c?.evaluation_hint ? { evaluation_hint: c.evaluation_hint } : {}),
  }));
  return makeOk({
    promptVersion: data.prompt_version ?? '',
    cards,
    dropped: data.dropped ?? 0,
    maxCards: data.max_cards ?? cards.length,
    context: {
      categories: data.context?.categories ?? 0,
      poolDigestChars: data.context?.pool_digest_chars ?? 0,
      poolFactors: data.context?.pool_factors ?? 0,
      model: data.context?.model ?? null,
    },
  });
}

export interface BatchDispatchReceipt {
  index: number;
  taskId: string | null;
  status: 'running' | 'queued' | 'failed';
  queuePosition: number | null;
  directionPreview: string;
  error: string | null;
}

export interface BatchDispatchResult {
  items: BatchDispatchReceipt[];
  started: number;
  queued: number;
  failed: number;
}

export interface BatchDispatchParams {
  directions: string[];
  market?: string;
  universe?: string;
  loopN?: number;
}

/** 批量派发：逐条排队成任务；运行期失败逐条回传（HTTP 恒 200）。 */
export async function dispatchMiningBatch(
  params: BatchDispatchParams,
): Promise<ApiResponse<BatchDispatchResult>> {
  const res = await apiClient.post('/alpha-agent/mining/batch', {
    directions: params.directions,
    market: params.market || 'a_share',
    universe: params.universe || 'csi300',
    ...(params.loopN ? { loop_n: params.loopN } : {}),
  });
  const data = res.data?.data ?? {};
  const items: BatchDispatchReceipt[] = (data.items ?? []).map((it: any) => ({
    index: it?.index ?? 0,
    taskId: it?.task_id ?? null,
    status: it?.status ?? 'failed',
    queuePosition: typeof it?.queue_position === 'number' ? it.queue_position : null,
    directionPreview: it?.direction_preview ?? '',
    error: it?.error ?? null,
  }));
  return makeOk({
    items,
    started: data.started ?? 0,
    queued: data.queued ?? 0,
    failed: data.failed ?? 0,
  });
}

export async function listTasks(): Promise<ApiResponse<{ tasks: Task[] }>> {
  const res = await apiClient.get(`/alpha-agent/tasks`);
  const tasks: Task[] = (res.data?.data?.tasks ?? []).map((t: any) =>
    normalizeAgentTask(t),
  );
  return makeOk({ tasks });
}

// ========================== Mining History API ==========================

/**
 * 任务状态（PG `rd_agent_mining_tasks`）。与 launcher 内存状态**不同名**：
 * 任务有 cancelled、没有 idle——不要拿 Task['status'] 去套历史行。
 */
export type MiningHistoryStatus =
  | 'pending'
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled';

/** 历史行：direction / status / factor_count 全部来自 PG，重启不失忆。 */
export interface MiningHistoryRow {
  task_id: string;
  user_id: string;
  market: string;
  universe: string;
  data_source: string;
  direction: string;
  /** text=文字指令 / doc=文档解析链（P1）/ legacy=历史回填 */
  source: string;
  doc_id: string | null;
  status: MiningHistoryStatus;
  progress_pct: number;
  current_loop: number;
  loop_n: number;
  error: string | null;
  factor_count: number;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

export interface MiningHistoryParams {
  market?: string;
  status?: string;
  limit?: number;
  offset?: number;
}

export const MINING_HISTORY_PAGE_SIZE = 50;

export async function getMiningHistory(
  params: MiningHistoryParams = {},
): Promise<ApiResponse<{ tasks: MiningHistoryRow[]; total: number }>> {
  const qs = new URLSearchParams();
  // 过滤参数只在有值时出现：undefined 不能变成 "undefined" 打到后端
  //（后端把未知状态当 400，乱串会让历史页整页报错）。
  if (params.market) qs.set('market', params.market);
  if (params.status) qs.set('status', params.status);
  qs.set('limit', String(params.limit ?? MINING_HISTORY_PAGE_SIZE));
  qs.set('offset', String(params.offset ?? 0));
  const res = await apiClient.get(`/alpha-agent/tasks/history?${qs.toString()}`);
  const data = res.data?.data ?? {};
  return makeOk({
    tasks: (data.tasks ?? []) as MiningHistoryRow[],
    // total 是过滤后的全量 COUNT（后端 count_history），分页器用它
    total: (data.total ?? 0) as number,
  });
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
  /** 只列该挖掘任务产出的因子（结果区权威清单：挖到多少列多少） */
  taskId?: string;
}

export interface FactorQualityCounts {
  high: number;
  medium: number;
  low: number;
  unknown: number;
}

export interface FactorListResponse {
  factors: Factor[];
  total: number;
  limit: number;
  offset: number;
  metadata?: any;
  libraries?: string[];
  /** 服务端本页实际上限（界面据此显示「已达上限」） */
  serverLimit?: number;
  /**
   * 同一过滤域内的**全量**质量分档计数（与 factors 窗口长度解耦）。
   * 统计瓦片必须用这份数字——拿窗口长度当总数会制造「越挖、中等因子越少」
   * 的假象（窗口滑动挤出老因子，不是质量真的下降）。旧后端可能缺省。
   */
  qualityCounts?: FactorQualityCounts | null;
}

/** 与服务端 Query(le=500) 对齐的客户端上限（请求超过会被 422）。 */
export const FACTOR_LIST_MAX_LIMIT = 500;

export async function getFactors(
  params: FactorListParams = {},
): Promise<ApiResponse<FactorListResponse>> {
  const qs = new URLSearchParams();
  // Backend caps `limit` at 500 — clamp client-side so callers requesting more
  // get the first 500 instead of a 422 validation error.
  const requested = params.limit ?? FACTOR_LIST_MAX_LIMIT;
  const clamped = Math.min(Math.max(requested, 1), FACTOR_LIST_MAX_LIMIT);
  qs.set('limit', String(clamped));
  // 服务端分页（created_at DESC 最新窗口上的 offset）——旧实现把 offset 只做
  // 本地 slice，既拿不到窗口外的行，又会把服务端返回的整页再切一次（offset>0 时切空）。
  const offset = Math.max(params.offset ?? 0, 0);
  if (offset > 0) qs.set('offset', String(offset));
  if (params.market) qs.set('market', params.market);
  if (params.universe) qs.set('universe', params.universe);
  if (params.taskId) qs.set('task_id', params.taskId);
  const res = await apiClient.get(`/alpha-agent/factors?${qs.toString()}`);
  let factors: Factor[] = (res.data?.data?.factors ?? []).map(normalizeAgentFactor);
  const serverLimit: number = res.data?.data?.limit ?? clamped;

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
  // total：无客户端过滤时用服务端**全量**口径（与窗口长度解耦）；带
  // quality/search 这类客户端过滤时只能如实返回过滤后行数。服务端缺字段
  // （旧后端/异常载荷）退回窗口长度——宁可退回旧口径，不编造数字。
  const serverTotal = res.data?.data?.total;
  const hasClientFilter = Boolean(params.quality || params.search);
  const total =
    !hasClientFilter && typeof serverTotal === 'number' ? serverTotal : factors.length;

  return makeOk({
    factors,
    total,
    limit: params.limit ?? total,
    offset,
    libraries: ['default'],
    serverLimit,
    qualityCounts: res.data?.data?.quality_counts ?? null,
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
      // 本端点两种返回（已触发 / 已在跑）都是 status="backtesting"——直接透传会落进
      // normalizeTaskStatus 的 default 分支变成 idle；显式归一为 running。
      status: 'running',
      progress: data.message,
    }),
  });
}

export interface BacktestStatusData {
  task: Task;
  /** 失败/取消原文（metadata.backtest_error 尾段，或 HTTP detail）——行内状态展示用 */
  error?: string;
}

/** 回测指标键词表（camelCase）；缺失一律不写键、页面显「—」，禁止补 0 */
export type BacktestMetricKey =
  | 'ic'
  | 'icir'
  | 'rankIc'
  | 'rankIcir'
  | 'annualReturn'
  | 'sharpeRatio'
  | 'maxDrawdown'
  | 'rre'
  | 'pfs'
  | 'pfsGauss'
  | 'pfsT'
  | 'turnoverDaily'
  | 'annTurnover'
  | 'annReturnNet'
  | 'sharpeNet'
  | 'maxDrawdownNet'
  | 'nObs';

/**
 * 因子行 / 回测历史行 → camelCase 指标（两处共用同一套映射）。
 *
 * - ICIR / Rank ICIR：qlib 路径表字段与 metadata_json 双写；H5 路径只在表字段。
 *   两个源都认（旧实现只读 metadata，H5 因子的 ICIR 永远显示不出来）。
 * - 机构级指标（mining_plugins 评估器链 → metadata）：缺失一律不写键，
 *   页面以「—」呈现——换手 0 与没算过是两回事，禁止补 0。
 * - 所有数值一律过 pickNumber 归一（字符串数字转换、NaN 视为缺失）——
 *   直接赋值会把 "0.03"/NaN 带进界面，对比高亮按 `typeof === number` 判缺席。
 */
export function extractBacktestMetrics(
  raw: any,
): Partial<Record<BacktestMetricKey, number>> {
  const metrics: Partial<Record<BacktestMetricKey, number>> = {};
  const meta = raw.metadata ?? {};
  const setMetric = (key: BacktestMetricKey, ...sources: any[]) => {
    const value = pickNumber(...sources);
    if (value != null) metrics[key] = value;
  };
  setMetric('ic', raw.ic_value);
  setMetric('sharpeRatio', raw.sharpe_ratio);
  setMetric('annualReturn', raw.annual_return);
  setMetric('maxDrawdown', raw.max_drawdown);
  setMetric('rankIc', raw.rank_ic);
  setMetric('icir', raw.icir, meta.icir);
  setMetric('rankIcir', raw.rank_icir, meta.rank_icir);
  setMetric('rre', meta.rre);
  setMetric('nObs', meta.n_obs);
  const quality = meta.quality ?? {};
  setMetric('pfs', quality.pfs);
  setMetric('pfsGauss', quality.pfs_gauss);
  setMetric('pfsT', quality.pfs_t);
  setMetric('turnoverDaily', meta.turnover_daily);
  setMetric('annTurnover', meta.ann_turnover);
  setMetric('annReturnNet', meta.ann_return_net);
  setMetric('sharpeNet', meta.sharpe_net);
  setMetric('maxDrawdownNet', meta.max_drawdown_net);
  return metrics;
}

export async function getBacktestStatus(
  taskId: string,
): Promise<ApiResponse<BacktestStatusData>> {
  try {
    const res = await apiClient.get(`/alpha-agent/factors/${taskId}`);
    const raw = res.data?.data ?? {};
    // 状态映射以 factor 行 status 为准。'pending'（从未回测）必须映射为 idle——
    // 旧实现把一切非终态映射成 running，attach 一个没回测过的因子会永远「回测中」。
    const rawStatus: string = raw.status ?? '';
    const status: TaskStatus =
      rawStatus === 'completed'
        ? 'completed'
        : rawStatus === 'failed' || rawStatus === 'cancelled'
          ? 'failed'
          : rawStatus === 'backtesting'
            ? 'running'
            : 'idle';
    const backtestError =
      typeof raw.metadata?.backtest_error === 'string' && raw.metadata.backtest_error
        ? (raw.metadata.backtest_error as string)
        : undefined;
    const metrics = extractBacktestMetrics(raw);
    return makeOk({
      task: normalizeAgentTask({
        task_id: taskId,
        status,
        progress:
          status === 'failed'
            ? backtestError ?? '回测失败'
            : status === 'completed'
              ? 'Backtest done'
              : status === 'running'
                ? 'Running'
                : '未回测',
        metrics: Object.keys(metrics).length > 0 ? metrics : undefined,
      }),
      error: backtestError,
    });
  } catch (err: any) {
    // 归属校验 404 / 网络错：把原文带给调用方（行内三态要能显示失败原因，不能只吞）
    const detail = err?.response?.data?.detail;
    const message = typeof detail === 'string' && detail ? detail : '查询回测状态失败';
    return makeOk({
      task: normalizeAgentTask({ task_id: taskId, status: 'failed', progress: message }),
      error: message,
    });
  }
}

export async function cancelBacktest(taskId: string): Promise<ApiResponse> {
  // taskId 即 factorId（回测任务以因子为句柄）；旧实现是空 stub——
  // 页面点了「停止回测」后端子进程照跑，属假动作。
  const res = await apiClient.post(`/alpha-agent/factors/${taskId}/cancel`);
  return makeOk(res.data?.data ?? {});
}

// ========================== 回测历史（一次运行一行） ==========================

/** 一次回测运行的历史记录（后端 rd_agent_factor_backtests 行）。 */
export interface BacktestHistoryRun {
  runId: string;
  status: string;
  market: string | null;
  universe: string | null;
  dataSource: string | null;
  dateRange: string | null;
  /** 发起时间（ISO，带 Z） */
  startedAt: string;
  /** 收口时间；未收口（进行中）为 null */
  finishedAt: string | null;
  error: string | null;
  /** camelCase 指标（与 getBacktestStatus 同一套映射；缺失不写键） */
  metrics: Partial<Record<BacktestMetricKey, number>>;
}

const BACKTEST_HISTORY_MAX_LIMIT = 100;

export async function listFactorBacktests(
  factorId: string,
  limit = 20,
): Promise<ApiResponse<{ runs: BacktestHistoryRun[] }>> {
  try {
    const clamped = Math.min(Math.max(Math.floor(limit) || 1, 1), BACKTEST_HISTORY_MAX_LIMIT);
    const res = await apiClient.get(
      `/alpha-agent/factors/${encodeURIComponent(factorId)}/backtests?limit=${clamped}`,
    );
    const rows: any[] = res.data?.data?.runs ?? [];
    const runs: BacktestHistoryRun[] = rows.map((row) => ({
      runId: row.run_id,
      status: row.status ?? '',
      market: row.market ?? null,
      universe: row.universe ?? null,
      dataSource: row.data_source ?? null,
      dateRange: row.date_range ?? null,
      startedAt: row.created_at ?? '',
      finishedAt: row.finished_at ?? null,
      error: typeof row.error === 'string' && row.error ? row.error : null,
      metrics: extractBacktestMetrics(row),
    }));
    return makeOk({ runs });
  } catch (err: any) {
    // 归属 404 / 网络错都带原文回来——历史面板要显示错误，不能静默空表
    const detail = err?.response?.data?.detail;
    const message = typeof detail === 'string' && detail ? detail : '查询回测历史失败';
    return { success: false, error: message } as ApiResponse<any>;
  }
}

// ========================== LLM Config ==========================

/**
 * 向量检索（embedding）通道状态。与 chat **互相独立**：它只反映用户自己填的值，
 * 为空表示沿用容器级 `EMBEDDING_*`，不跟随 chat 的 env 兜底。
 * 唯一的运行时消费者是因子挖掘（RD-Agent 子进程的记忆检索）。
 */
export interface EmbeddingConfigStatus {
  model: string;
  base_url: string;
  has_key: boolean;
  /** 掩码后的 Key（如 `sk-****abcd`）；Key 过短时为空串，绝不回显明文 */
  key_masked: string;
}

export interface LlmConfigStatus {
  configured: boolean;
  reason?: string;
  /** 配置来源：env=服务器环境变量，user_profile=个人中心 AI 服务配置 */
  source?: 'env' | 'user_profile';
  provider?: string;
  model?: string;
  base_url?: string;
  api_key_masked?: string;
  /** 与 chat 独立，故 chat 未配置时这一段照样返回 */
  embedding?: EmbeddingConfigStatus;
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

/**
 * 保存向量检索配置。
 *
 * **只提交显式传入的字段**：`undefined` = 不动，`''` = 清除（回退容器级
 * `EMBEDDING_*`）。全量提交会让「只改模型名」的请求顺手清掉已存的 Key，
 * 子进程随即退回容器默认端点——而没有任何一层会报错。
 */
export async function saveEmbeddingConfig(embedding: {
  model?: string;
  baseUrl?: string;
  apiKey?: string;
}): Promise<ApiResponse<EmbeddingConfigStatus>> {
  const payload: Record<string, string> = {};
  if (embedding.model !== undefined) payload.embedding_model = embedding.model;
  if (embedding.baseUrl !== undefined) payload.embedding_base_url = embedding.baseUrl;
  if (embedding.apiKey !== undefined) payload.embedding_api_key = embedding.apiKey;

  const res = await apiClient.put(`/alpha-agent/llm-config/embedding`, payload);
  return makeOk(res.data?.data);
}

// ========================== Health Check ==========================

export async function healthCheck(): Promise<
  ApiResponse<{ status: string; timestamp: string }>
> {
  await apiClient.get(`/alpha-agent/stats`);
  return makeOk({ status: 'ok', timestamp: new Date().toISOString() });
}

// ========================== Factor Pool API（P1 因子池） ==========================
//
// 作用域约定：`universe` 空串/缺省 = 全部（后端把它归一为 None），
// 非空 = 精确作用域。刷新端点的 owner 恒为登录身份，前端没有 user_id 参数。

export interface PoolOverview {
  total: number;
  withPanel: number;
  retrievedTotal: number;
  retrievedFactors: number;
  avgPoolScore: number | null;
  avgNovelty: number | null;
  avgMaxCorr: number | null;
  avgIc: number | null;
  avgIcir: number | null;
  avgPfs: number | null;
  /** 池多样性熵（0..1，越高越分散）；未算过 = null → 界面显「—」 */
  poolDiversity: number | null;
  /** 有效因子数（熵的指数形式） */
  nEff: number | null;
  /** 已归档因子数（默认聚合只算活跃因子；此数给「已归档 N」提示） */
  archivedCount: number;
}

export interface PoolGateOutcome {
  key: string;
  label: string;
  mode: 'soft' | 'hard' | string;
  status: 'pass' | 'fail' | 'skipped' | string;
  message: string;
  observed: number | null;
  threshold: number | null;
}

export interface PoolFactorGates {
  rejected: boolean;
  gates: PoolGateOutcome[];
}

export interface PoolFactorRow {
  factorId: string;
  factorName: string;
  factorFormulation: string;
  ic: number | null;
  rankIc: number | null;
  icir: number | null;
  pfs: number | null;
  poolScore: number | null;
  novelty: number | null;
  maxPoolCorr: number | null;
  maxPoolCorrWith: string | null;
  diversityContrib: number | null;
  timesRetrieved: number;
  lastRetrievedAt: string | null;
  hasPanel: boolean;
  createdAt: string | null;
  updatedAt: string | null;
  /** 归档时间戳（NULL = 活跃）；仅「含已归档」视图下有值 */
  archivedAt: string | null;
  /** 物化门禁裁决（metadata.materialization.gates）；未物化过 = null */
  gates: PoolFactorGates | null;
}

export interface PoolFactorList {
  total: number;
  items: PoolFactorRow[];
  limit: number;
  offset: number;
}

export interface PoolGraphNode {
  factorId: string;
  factorName: string;
  poolScore: number | null;
  novelty: number | null;
  timesRetrieved: number;
  hasPanel: boolean;
  taskId: string | null;
  icir: number | null;
}

export interface PoolGraphEdge {
  source: string;
  target: string;
  relation: string;
  method: string;
  weight: number | null;
}

export interface PoolGraph {
  nodes: PoolGraphNode[];
  edges: PoolGraphEdge[];
  maxNodes?: number;
}

export interface PoolRefreshStatus {
  status: string;
  running: boolean;
  log?: { path?: string; exists: boolean; lines: string[]; truncated?: boolean };
  args?: Record<string, unknown>;
  summary?: Record<string, unknown>;
  error?: string;
  started_at?: string;
  finished_at?: string;
  /** 最近一次刷新属主不是当前用户时的掩码状态（后端不回显日志/scope） */
  note?: string;
}

export interface MiningGateDescriptor {
  key: string;
  label: string;
  default_mode: 'soft' | 'hard' | string;
  description: string;
  default_threshold: number | null;
}

/**
 * 物化门禁描述符（与 /metrics/registry 的 gates 段同源）。
 * 后端失败回落空数组：门禁页显「描述符不可用」而不是假数据。
 */
export async function getGateDescriptors(): Promise<
  ApiResponse<{ gates: MiningGateDescriptor[] }>
> {
  try {
    const res = await apiClient.get(`/alpha-agent/metrics/registry`);
    const raw = res.data?.data?.gates;
    const gates: MiningGateDescriptor[] = (Array.isArray(raw) ? raw : []).map(
      (g: any) => ({
        key: String(g?.key ?? ''),
        label: String(g?.label ?? g?.key ?? ''),
        default_mode: String(g?.default_mode ?? 'soft'),
        description: String(g?.description ?? ''),
        default_threshold:
          typeof g?.default_threshold === 'number' && Number.isFinite(g.default_threshold)
            ? g.default_threshold
            : null,
      }),
    );
    return makeOk({ gates });
  } catch {
    return makeOk({ gates: [] });
  }
}

export type PoolSortKey =
  | 'pool_score'
  | 'novelty'
  | 'ic'
  | 'times_retrieved'
  | 'created_at'
  | 'updated_at';

function poolQs(market: string, universe?: string): string {
  const qs = new URLSearchParams();
  qs.set('market', market);
  if (universe) qs.set('universe', universe);
  return qs.toString();
}

function poolNum(value: unknown): number | null {
  return toNumber(value);
}

function mapPoolFactorRow(raw: any): PoolFactorRow {
  return {
    factorId: String(raw?.factor_id ?? ''),
    factorName: String(raw?.factor_name ?? 'unnamed'),
    factorFormulation: String(raw?.factor_formulation ?? ''),
    ic: poolNum(raw?.ic_value),
    rankIc: poolNum(raw?.rank_ic),
    icir: poolNum(raw?.icir),
    pfs: poolNum(raw?.pfs),
    poolScore: poolNum(raw?.pool_score),
    novelty: poolNum(raw?.novelty),
    maxPoolCorr: poolNum(raw?.max_pool_corr),
    maxPoolCorrWith: raw?.max_pool_corr_with != null ? String(raw.max_pool_corr_with) : null,
    diversityContrib: poolNum(raw?.diversity_contrib),
    timesRetrieved: Number.isFinite(raw?.times_retrieved) ? Number(raw.times_retrieved) : 0,
    lastRetrievedAt: raw?.last_retrieved_at != null ? String(raw.last_retrieved_at) : null,
    hasPanel: raw?.has_panel === true,
    createdAt: raw?.created_at != null ? String(raw.created_at) : null,
    updatedAt: raw?.updated_at != null ? String(raw.updated_at) : null,
    archivedAt: raw?.archived_at != null ? String(raw.archived_at) : null,
    gates:
      raw?.gates && Array.isArray(raw.gates?.gates)
        ? {
            rejected: raw.gates.rejected === true,
            gates: raw.gates.gates.map((g: any) => ({
              key: String(g?.key ?? ''),
              label: String(g?.label ?? g?.key ?? ''),
              mode: String(g?.mode ?? 'soft'),
              status: String(g?.status ?? 'skipped'),
              message: String(g?.message ?? ''),
              observed: poolNum(g?.observed),
              threshold: poolNum(g?.threshold),
            })),
          }
        : null,
  };
}

/** 池总览（KPI + 多样性熵）。失败返回 success=false，面板显错误而不是假 0。 */
export async function getPoolOverview(params: {
  market: string;
  universe?: string;
}): Promise<ApiResponse<PoolOverview>> {
  try {
    const res = await apiClient.get(
      `/alpha-agent/pool/overview?${poolQs(params.market, params.universe)}`,
    );
    const raw = res.data?.data ?? {};
    return makeOk({
      total: Number(raw.total ?? 0),
      withPanel: Number(raw.with_panel ?? 0),
      retrievedTotal: Number(raw.retrieved_total ?? 0),
      retrievedFactors: Number(raw.retrieved_factors ?? 0),
      avgPoolScore: poolNum(raw.avg_pool_score),
      avgNovelty: poolNum(raw.avg_novelty),
      avgMaxCorr: poolNum(raw.avg_max_corr),
      avgIc: poolNum(raw.avg_ic),
      avgIcir: poolNum(raw.avg_icir),
      avgPfs: poolNum(raw.avg_pfs),
      poolDiversity: poolNum(raw.pool_diversity),
      nEff: poolNum(raw.n_eff),
      archivedCount: Number(raw.archived_count ?? 0) || 0,
    });
  } catch (error: unknown) {
    const message = error instanceof Error ? error.message : '因子池总览获取失败';
    return { success: false, error: message };
  }
}

/** 池内因子分页列表（含门禁裁决、被检索次数、面板标记）。 */
export async function getPoolFactors(params: {
  market: string;
  universe?: string;
  limit?: number;
  offset?: number;
  sort?: PoolSortKey;
  /** 勾选「含已归档」才带出归档行（行里 archivedAt 有值） */
  includeArchived?: boolean;
}): Promise<ApiResponse<PoolFactorList>> {
  try {
    const qs = new URLSearchParams(poolQs(params.market, params.universe));
    qs.set('limit', String(params.limit ?? 50));
    qs.set('offset', String(params.offset ?? 0));
    if (params.sort) qs.set('sort', params.sort);
    if (params.includeArchived) qs.set('include_archived', 'true');
    const res = await apiClient.get(`/alpha-agent/pool/factors?${qs.toString()}`);
    const raw = res.data?.data ?? {};
    return makeOk({
      total: Number(raw.total ?? 0),
      items: Array.isArray(raw.items) ? raw.items.map(mapPoolFactorRow) : [],
      limit: Number(raw.limit ?? 50),
      offset: Number(raw.offset ?? 0),
    });
  } catch (error: unknown) {
    const message = error instanceof Error ? error.message : '池因子列表获取失败';
    return { success: false, error: message };
  }
}

/** 谱系图（nodes + edges；节点按 pool_score 取前 max_nodes 个）。 */
export async function getPoolGraph(params: {
  market: string;
  universe?: string;
  maxNodes?: number;
  includeArchived?: boolean;
}): Promise<ApiResponse<PoolGraph>> {
  try {
    const qs = new URLSearchParams(poolQs(params.market, params.universe));
    qs.set('max_nodes', String(params.maxNodes ?? 200));
    if (params.includeArchived) qs.set('include_archived', 'true');
    const res = await apiClient.get(`/alpha-agent/pool/graph?${qs.toString()}`);
    const raw = res.data?.data ?? {};
    return makeOk({
      nodes: (Array.isArray(raw.nodes) ? raw.nodes : []).map((n: any) => ({
        factorId: String(n?.factor_id ?? ''),
        factorName: String(n?.factor_name ?? 'unnamed'),
        poolScore: poolNum(n?.pool_score),
        novelty: poolNum(n?.novelty),
        timesRetrieved: Number(n?.times_retrieved ?? 0),
        hasPanel: n?.has_panel === true,
        taskId: n?.task_id != null ? String(n.task_id) : null,
        icir: poolNum(n?.icir),
      })),
      edges: (Array.isArray(raw.edges) ? raw.edges : []).map((e: any) => ({
        source: String(e?.src_factor_id ?? ''),
        target: String(e?.dst_factor_id ?? ''),
        relation: String(e?.relation ?? ''),
        method: String(e?.method ?? ''),
        weight: poolNum(e?.weight),
      })),
    });
  } catch (error: unknown) {
    const message = error instanceof Error ? error.message : '谱系图获取失败';
    return { success: false, error: message };
  }
}

/**
 * 启动后台刷新（默认 dry_run 预演）。409 = 已有刷新在跑（单飞闸门），
 * 400 = 参数被白名单拒绝，500 = 子进程起不来——都映射成可展示的 error。
 */
export async function refreshPool(opts: {
  market: string;
  universe?: string;
  dryRun: boolean;
}): Promise<ApiResponse<{ started: boolean; pid?: number; logPath?: string; confirmed?: boolean }>> {
  try {
    const qs = new URLSearchParams(poolQs(opts.market, opts.universe));
    qs.set('dry_run', String(opts.dryRun));
    const res = await apiClient.post(`/alpha-agent/pool/refresh?${qs.toString()}`);
    const raw = res.data?.data ?? {};
    return makeOk({
      started: raw.started === true,
      pid: typeof raw.pid === 'number' ? raw.pid : undefined,
      logPath: raw.log_path != null ? String(raw.log_path) : undefined,
      confirmed: raw.confirmed === true,
    });
  } catch (error: unknown) {
    const err = error as { response?: { status?: number; data?: { detail?: unknown } } };
    const status = err?.response?.status;
    const detail = err?.response?.data?.detail;
    const message =
      typeof detail === 'string' && detail
        ? detail
        : status === 409
          ? '已有因子池刷新任务在运行，请稍后再试'
          : status === 403
            ? '没有执行刷新的权限'
            : status === 500
              ? '刷新子进程启动失败，请查看服务日志'
              : '刷新请求失败';
    return { success: false, error: message };
  }
}

/** 刷新状态（锁探活 + 最近一次落盘状态 + 日志尾）。 */
export async function getPoolRefreshStatus(): Promise<ApiResponse<PoolRefreshStatus>> {
  try {
    const res = await apiClient.get(`/alpha-agent/pool/refresh/status`);
    const raw = res.data?.data ?? {};
    const log = raw.log ?? {};
    return makeOk({
      status: String(raw.status ?? 'unknown'),
      running: raw.running === true,
      log: {
        path: log.path != null ? String(log.path) : undefined,
        exists: log.exists === true,
        lines: Array.isArray(log.lines) ? log.lines.map(String) : [],
        truncated: log.truncated === true,
      },
      args: raw.args ?? undefined,
      summary: raw.summary ?? undefined,
      error: raw.error != null ? String(raw.error) : undefined,
      started_at: raw.started_at != null ? String(raw.started_at) : undefined,
      finished_at: raw.finished_at != null ? String(raw.finished_at) : undefined,
      note: raw.note != null ? String(raw.note) : undefined,
    });
  } catch {
    // 状态查询失败不抛：面板显示「未知」，不影响池数据本身
    return makeOk({ status: 'unknown', running: false });
  }
}

// ── 清理建议（P3）：只建议不自动删；归档 = 时间戳，随时可恢复 ──────────

export interface PoolCleanupReason {
  code: string;
  label: string;
  detail: string;
}

export interface PoolCleanupSuggestion {
  factorId: string;
  factorName: string;
  factorFormulation: string;
  icir: number | null;
  poolScore: number | null;
  maxPoolCorr: number | null;
  maxPoolCorrWith: string | null;
  diversityContrib: number | null;
  timesRetrieved: number;
  severity: 'high' | 'medium' | string;
  reasons: PoolCleanupReason[];
}

export interface PoolCleanupCriteria {
  corrDup: number;
  weakIcirQuantile: number;
  minIcirSample: number;
  /** 池内后 q 分位 ICIR 实际阈值；样本不足/无分化 = null → 显「—」 */
  weakIcirThreshold: number | null;
  icirSampleSize: number;
}

export interface PoolCleanupSota {
  count: number;
  bestIc: number | null;
  bestIcir: number | null;
  bestPfs: number | null;
}

export interface PoolCleanupReport {
  /** 截断到 limit 的建议（排序：high 在前，再按 factor_id） */
  items: PoolCleanupSuggestion[];
  /** 全量建议数（不代表 items.length） */
  total: number;
  /** 参与评估的活跃因子数 */
  poolSize: number;
  archivedCount: number;
  /** 全量建议的判据计数（code → 条数） */
  summary: Record<string, number>;
  criteria: PoolCleanupCriteria;
  sota: PoolCleanupSota;
}

export interface PoolArchiveResult {
  archived: number;
  archivedIds: string[];
  skipped: string[];
}

export interface PoolUnarchiveResult {
  restored: number;
  restoredIds: string[];
  skipped: string[];
}

/** 清理建议（判据逐条带数字证据）。失败返回 success=false，面板显错误。 */
export async function getPoolCleanupSuggestions(params: {
  market: string;
  universe?: string;
  limit?: number;
}): Promise<ApiResponse<PoolCleanupReport>> {
  try {
    const qs = new URLSearchParams(poolQs(params.market, params.universe));
    qs.set('limit', String(params.limit ?? 50));
    const res = await apiClient.get(
      `/alpha-agent/pool/cleanup/suggestions?${qs.toString()}`,
    );
    const raw = res.data?.data ?? {};
    const c = raw.criteria ?? {};
    const s = raw.sota ?? {};
    return makeOk({
      items: (Array.isArray(raw.items) ? raw.items : []).map((it: any) => ({
        factorId: String(it?.factor_id ?? ''),
        factorName: String(it?.factor_name ?? 'unnamed'),
        factorFormulation: String(it?.factor_formulation ?? ''),
        icir: poolNum(it?.icir),
        poolScore: poolNum(it?.pool_score),
        maxPoolCorr: poolNum(it?.max_pool_corr),
        maxPoolCorrWith:
          it?.max_pool_corr_with != null ? String(it.max_pool_corr_with) : null,
        diversityContrib: poolNum(it?.diversity_contrib),
        timesRetrieved: Number.isFinite(it?.times_retrieved)
          ? Number(it.times_retrieved)
          : 0,
        severity: String(it?.severity ?? 'medium'),
        reasons: (Array.isArray(it?.reasons) ? it.reasons : []).map((r: any) => ({
          code: String(r?.code ?? ''),
          label: String(r?.label ?? r?.code ?? ''),
          detail: String(r?.detail ?? ''),
        })),
      })),
      total: Number(raw.total ?? 0),
      poolSize: Number(raw.pool_size ?? 0),
      archivedCount: Number(raw.archived_count ?? 0),
      summary:
        raw.summary && typeof raw.summary === 'object' ? raw.summary : {},
      criteria: {
        corrDup: poolNum(c.corr_dup) ?? 0.9,
        weakIcirQuantile: poolNum(c.weak_icir_quantile) ?? 0.2,
        minIcirSample: Number.isFinite(c.min_icir_sample)
          ? Number(c.min_icir_sample)
          : 5,
        weakIcirThreshold: poolNum(c.weak_icir_threshold),
        icirSampleSize: Number.isFinite(c.icir_sample_size)
          ? Number(c.icir_sample_size)
          : 0,
      },
      sota: {
        count: Number(s.count ?? 0),
        bestIc: poolNum(s.best_ic),
        bestIcir: poolNum(s.best_icir),
        bestPfs: poolNum(s.best_pfs),
      },
    });
  } catch (error: unknown) {
    const message = error instanceof Error ? error.message : '清理建议获取失败';
    return { success: false, error: message };
  }
}

/** 批量归档因子（非删除）。跨用户/不在池/已归档 id 只进 skipped。 */
export async function archivePoolFactors(factorIds: string[]): Promise<
  ApiResponse<PoolArchiveResult>
> {
  try {
    const res = await apiClient.post(`/alpha-agent/pool/cleanup/archive`, {
      factor_ids: factorIds,
    });
    const raw = res.data?.data ?? {};
    return makeOk({
      archived: Number(raw.archived ?? 0),
      archivedIds: Array.isArray(raw.archived_ids)
        ? raw.archived_ids.map(String)
        : [],
      skipped: Array.isArray(raw.skipped) ? raw.skipped.map(String) : [],
    });
  } catch (error: unknown) {
    const message = error instanceof Error ? error.message : '归档失败';
    return { success: false, error: message };
  }
}

/** 恢复归档（清 archived_at），重新参与注入与池视图。 */
export async function unarchivePoolFactors(factorIds: string[]): Promise<
  ApiResponse<PoolUnarchiveResult>
> {
  try {
    const res = await apiClient.post(`/alpha-agent/pool/cleanup/unarchive`, {
      factor_ids: factorIds,
    });
    const raw = res.data?.data ?? {};
    return makeOk({
      restored: Number(raw.restored ?? 0),
      restoredIds: Array.isArray(raw.restored_ids)
        ? raw.restored_ids.map(String)
        : [],
      skipped: Array.isArray(raw.skipped) ? raw.skipped.map(String) : [],
    });
  } catch (error: unknown) {
    const message = error instanceof Error ? error.message : '恢复归档失败';
    return { success: false, error: message };
  }
}

// ========================== Combo Lab API（P2 组合实验室） ==========================
//
// 组合作业是后台子进程：POST 建行 + 启动即时回包（不阻塞请求），前端按
// combo_id 轮询详情看 pending/running/done/failed。权重是 `{factor_id: 权重}`
// 对象（L1 归一 Σ|w|=1，允许负权=反向暴露）；缺失指标一律 null → 界面显「—」。

export interface ComboConfigEcho {
  seed: number | null;
  popsize: number | null;
  maxiter: number | null;
  tol: number | null;
  maxDays: number | null;
  trainRatio: number | null;
  timeBudgetS: number | null;
  costRate: number | null;
  converged: boolean | null;
  nEvaluations: number | null;
}

export interface ComboCurve {
  /** 交易日（ISO 或 YYYY-MM-DD） */
  dates: string[];
  /** 扣成本净值（1.0 起累计） */
  values: number[];
}

export interface ComboWindowMetrics {
  meanRankIc: number | null;
  rankIcir: number | null;
  turnoverDaily: number | null;
  annTurnover: number | null;
  annReturnNet: number | null;
  sharpeNet: number | null;
  maxDrawdownNet: number | null;
  nDays: number | null;
  nObs: number | null;
  /** 仅 valid 窗：扣成本净值曲线；train 窗为 null */
  curve: ComboCurve | null;
  /** 仅 train 窗：优化参数回执（seed 落库可复现）；valid 窗为 null */
  config: ComboConfigEcho | null;
}

export interface ComboListItem {
  comboId: string;
  name: string;
  market: string;
  universe: string;
  nFactors: number;
  /** pending | running | done | failed */
  status: string;
  error: string | null;
  trainWindow: string | null;
  trainMeanRankIc: number | null;
  validMeanRankIc: number | null;
  createdAt: string | null;
  updatedAt: string | null;
}

export interface ComboList {
  total: number;
  items: ComboListItem[];
}

export interface ComboDetail {
  comboId: string;
  market: string;
  universe: string;
  name: string;
  factorIds: string[];
  /** factor_id → 权重（L1 归一；正=正向暴露，负=反向暴露） */
  weights: Record<string, number>;
  trainWindow: string | null;
  trainMetrics: ComboWindowMetrics | null;
  validMetrics: ComboWindowMetrics | null;
  status: string;
  error: string | null;
  createdAt: string | null;
  updatedAt: string | null;
}

function mapComboCurve(raw: any): ComboCurve | null {
  if (!raw || !Array.isArray(raw.dates) || !Array.isArray(raw.values)) return null;
  if (raw.dates.length === 0 || raw.dates.length !== raw.values.length) return null;
  const values = raw.values.map((v: unknown) => toNumber(v));
  if (values.some((v: number | null) => v == null)) return null;
  return { dates: raw.dates.map(String), values: values as number[] };
}

function mapComboConfigEcho(raw: any): ComboConfigEcho | null {
  if (!raw || typeof raw !== 'object') return null;
  return {
    seed: poolNum(raw.seed),
    popsize: poolNum(raw.popsize),
    maxiter: poolNum(raw.maxiter),
    tol: poolNum(raw.tol),
    maxDays: poolNum(raw.max_days),
    trainRatio: poolNum(raw.train_ratio),
    timeBudgetS: poolNum(raw.time_budget_s),
    costRate: poolNum(raw.cost_rate),
    converged: typeof raw.converged === 'boolean' ? raw.converged : null,
    nEvaluations: poolNum(raw.n_evaluations),
  };
}

function mapComboWindowMetrics(raw: any): ComboWindowMetrics | null {
  if (!raw || typeof raw !== 'object') return null;
  return {
    meanRankIc: poolNum(raw.mean_rank_ic),
    rankIcir: poolNum(raw.rank_icir),
    turnoverDaily: poolNum(raw.turnover_daily),
    annTurnover: poolNum(raw.ann_turnover),
    annReturnNet: poolNum(raw.ann_return_net),
    sharpeNet: poolNum(raw.sharpe_net),
    maxDrawdownNet: poolNum(raw.max_drawdown_net),
    nDays: poolNum(raw.n_days),
    nObs: poolNum(raw.n_obs),
    curve: mapComboCurve(raw.curve),
    config: mapComboConfigEcho(raw.config),
  };
}

function mapComboListItem(raw: any): ComboListItem {
  return {
    comboId: String(raw?.combo_id ?? ''),
    name: String(raw?.name ?? ''),
    market: String(raw?.market ?? ''),
    universe: String(raw?.universe ?? ''),
    nFactors: Number.isFinite(raw?.n_factors) ? Number(raw.n_factors) : 0,
    status: String(raw?.status ?? 'unknown'),
    error: raw?.error != null ? String(raw.error) : null,
    trainWindow: raw?.train_window != null ? String(raw.train_window) : null,
    trainMeanRankIc: poolNum(raw?.train_mean_rank_ic),
    validMeanRankIc: poolNum(raw?.valid_mean_rank_ic),
    createdAt: raw?.created_at != null ? String(raw.created_at) : null,
    updatedAt: raw?.updated_at != null ? String(raw.updated_at) : null,
  };
}

function mapComboDetail(raw: any): ComboDetail {
  const weights: Record<string, number> = {};
  if (raw?.weights && typeof raw.weights === 'object' && !Array.isArray(raw.weights)) {
    for (const [fid, w] of Object.entries(raw.weights)) {
      const value = poolNum(w);
      if (value != null) weights[fid] = value;
    }
  }
  return {
    comboId: String(raw?.combo_id ?? ''),
    market: String(raw?.market ?? ''),
    universe: String(raw?.universe ?? ''),
    name: String(raw?.name ?? ''),
    factorIds: Array.isArray(raw?.factor_ids) ? raw.factor_ids.map(String) : [],
    weights,
    trainWindow: raw?.train_window != null ? String(raw.train_window) : null,
    trainMetrics: mapComboWindowMetrics(raw?.train_metrics),
    validMetrics: mapComboWindowMetrics(raw?.valid_metrics),
    status: String(raw?.status ?? 'unknown'),
    error: raw?.error != null ? String(raw.error) : null,
    createdAt: raw?.created_at != null ? String(raw.created_at) : null,
    updatedAt: raw?.updated_at != null ? String(raw.updated_at) : null,
  };
}

/**
 * 提交组合优化（后台子进程）。400 = 因子集被拒（detail 是原因，如无面板/
 * 不足 2 个），409 = 已有组合作业在跑（单飞），500 = 子进程起不来——
 * 都映射成可展示的 error 文案。
 */
export async function optimizeCombo(params: {
  market: string;
  universe?: string;
  factorIds: string[];
  name?: string;
  seed?: number | null;
}): Promise<
  ApiResponse<{
    comboId: string;
    status: string;
    pid?: number;
    logPath?: string;
    confirmed?: boolean;
  }>
> {
  try {
    const res = await apiClient.post(`/alpha-agent/combos/optimize`, {
      market: params.market,
      universe: params.universe ?? '',
      factor_ids: params.factorIds,
      name: params.name ?? '',
      seed: params.seed ?? null,
    });
    const raw = res.data?.data ?? {};
    return makeOk({
      comboId: String(raw.combo_id ?? ''),
      status: String(raw.status ?? 'pending'),
      pid: typeof raw.pid === 'number' ? raw.pid : undefined,
      logPath: raw.log_path != null ? String(raw.log_path) : undefined,
      confirmed: raw.confirmed === true,
    });
  } catch (error: unknown) {
    const err = error as { response?: { status?: number; data?: { detail?: unknown } } };
    const status = err?.response?.status;
    const detail = err?.response?.data?.detail;
    const message =
      typeof detail === 'string' && detail
        ? detail
        : status === 409
          ? '已有组合优化任务在运行，请稍后再试'
          : status === 500
            ? '优化子进程启动失败，请查看服务日志'
            : '组合优化请求失败';
    return { success: false, error: message };
  }
}

/** 组合历史列表（user-scoped，新→旧）；market 缺省 = 全部市场。 */
export async function listCombos(
  params: { market?: string; limit?: number; offset?: number } = {},
): Promise<ApiResponse<ComboList>> {
  try {
    const qs = new URLSearchParams();
    if (params.market) qs.set('market', params.market);
    qs.set('limit', String(params.limit ?? 20));
    qs.set('offset', String(params.offset ?? 0));
    const res = await apiClient.get(`/alpha-agent/combos?${qs.toString()}`);
    const raw = res.data?.data ?? {};
    return makeOk({
      total: Number(raw.total ?? 0),
      items: Array.isArray(raw.items) ? raw.items.map(mapComboListItem) : [],
    });
  } catch (error: unknown) {
    const err = error as { response?: { data?: { detail?: unknown } } };
    const detail = err?.response?.data?.detail;
    const message =
      typeof detail === 'string' && detail
        ? detail
        : error instanceof Error
          ? error.message
          : '组合列表获取失败';
    return { success: false, error: message };
  }
}

/** 组合详情（含权重 / 两窗指标 / valid 净值曲线）；非属主 404。 */
export async function getCombo(comboId: string): Promise<ApiResponse<ComboDetail>> {
  try {
    const res = await apiClient.get(`/alpha-agent/combos/${encodeURIComponent(comboId)}`);
    return makeOk(mapComboDetail(res.data?.data ?? {}));
  } catch (error: unknown) {
    const err = error as { response?: { status?: number; data?: { detail?: unknown } } };
    const status = err?.response?.status;
    const detail = err?.response?.data?.detail;
    const message =
      status === 404
        ? '组合不存在或不属于当前用户'
        : typeof detail === 'string' && detail
          ? detail
          : '组合详情获取失败';
    return { success: false, error: message };
  }
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
  let lastQueuePos = -1;
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
  let lastFactorsHead = '';
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
      // 排队位次进变化检测：前面任务开跑时位次前移也要推送新文案
      const queuePos = typeof data.queue_position === 'number' ? data.queue_position : -1;
      const queuePosChanged = queuePos !== lastQueuePos;
      // 后端随任务状态返回的结构化因子（rd_agent_factors 已落库），优先于日志正则解析
      const backendFactors: any[] = Array.isArray(data.factors) ? data.factors : [];
      // 载荷恒为「最新 20 条」（ORDER BY created_at DESC）：数量饱和在 20 后，新因子
      // 只会顶掉最旧一条（数量不变）——只比数量会让新因子静默不进 UI；并列比首条 id。
      const factorsHead = backendFactors[0]?.factor_id ?? '';
      const factorsChanged =
        backendFactors.length !== lastFactorsCount || factorsHead !== lastFactorsHead;

      if (statusChanged || phaseChanged || pctChanged || factorsChanged || queuePosChanged) {
        lastStatus = status;
        lastPhase = backendPhase;
        lastPct = progressPct;
        lastQueuePos = queuePos;
        lastFactorsCount = backendFactors.length;
        lastFactorsHead = factorsHead;
        const phase: ExecutionPhase =
          status === 'completed'
            ? 'completed'
            : PHASE_MAP[backendPhase] || 'parsing';

        onMessage({
          type: 'progress',
          taskId,
          data: {
            phase,
            // 后端原始状态：排队任务绑定传输后，消费方不能凭 phase 把 queued 当 running
            status,
            currentRound,
            totalRounds,
            progress: progressPct,
            queuePosition: queuePos >= 0 ? queuePos : null,
            message:
              progressText ||
              (status === 'queued'
                ? `排队中${queuePos >= 0 ? `（第 ${queuePos} 位）` : ''}`
                : status),
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
